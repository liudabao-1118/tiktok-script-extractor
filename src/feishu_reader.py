#!/usr/bin/env python3
"""
Feishu (Lark) direct reader/writer for the TikTok Script Extractor.

Lets the GitHub Actions automation read TikTok links and (optionally) write
back transcripts/translations DIRECTLY to the Feishu spreadsheet -- no CSV
export needed. It uses a Feishu *self-built app* (App ID + App Secret) stored as
GitHub Secrets, not the WorkBuddy connector.

Required GitHub Secrets:
  FEISHU_APP_ID      - from the self-built app credentials
  FEISHU_APP_SECRET  - from the self-built app credentials
  FEISHU_WIKI_TOKEN  - the node token from the wiki URL
                      (https://xxx.feishu.cn/wiki/<THIS_PART>)

API references (VERIFIED live on 2026-08-12):
  - tenant token:  POST /open-apis/auth/v3/tenant_access_token/internal
  - wiki resolve:  GET  /open-apis/wiki/v2/spaces/get_node?token=&obj_type=wiki
  - list sheets:   GET  /open-apis/sheets/v3/spreadsheets/{token}/sheets/query
                   (returns data.sheets[].sheet_id  -- NOT "sheetId")
  - read values:   GET  /open-apis/sheets/v2/spreadsheets/{token}/values/{sheetId}!A1:D36
  - write values:  PUT  /open-apis/sheets/v2/spreadsheets/{token}/values
                   body {"valueRange":{"range":"{sheetId}!B5","values":[["text"]]}}

NOTE on cell format: a TikTok URL cell is returned as a *rich-text array*
    [{"type":"url","text":"https://...","link":"https://..."}], not a plain
    string. flatten_cell() handles both plain strings and rich-text arrays.
"""

import os
import re
import requests

BASE = "https://open.feishu.cn/open-apis"

FAILED_MARKERS = ("[download_failed]", "[analysis_failed]", "[unavailable]", "[提取失败]")
URL_RE = re.compile(r"https?://(www\.|vm\.|vt\.|m\.)?tiktok\.com", re.I)


def flatten_cell(v):
    """Convert a Feishu cell value into a plain string.

    Handles: None, plain str, rich-text list-of-dicts, or stray dict.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("link") or "")
            elif isinstance(item, str):
                parts.append(item)
        return "".join(p for p in parts if p)
    if isinstance(v, dict):
        return v.get("text") or v.get("link") or ""
    return str(v)


class FeishuClient:
    def __init__(self, app_id, app_secret, wiki_token, sheet_title="脚本读取",
                 max_rows=200):
        self.app_id = app_id
        self.app_secret = app_secret
        self.wiki_token = wiki_token
        self.sheet_title = sheet_title
        self.max_rows = max_rows
        self._token = None
        self.spreadsheet_token = None
        self.sheet_id = None

    # ---------- auth ----------
    def _get_token(self):
        if self._token:
            return self._token
        r = requests.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=30,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"tenant_access_token failed: {d.get('msg')} | {d}")
        # NOTE: the internal token endpoint returns the token at TOP LEVEL
        # (no "data" wrapper): {"code":0,"tenant_access_token":"t-...",...}
        self._token = d["tenant_access_token"]
        return self._token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ---------- resolve wiki -> spreadsheet -> sheet ----------
    def resolve(self):
        """Resolve the wiki node token to a spreadsheet token."""
        self._get_token()
        r = requests.get(
            f"{BASE}/wiki/v2/spaces/get_node",
            params={"token": self.wiki_token, "obj_type": "wiki"},
            headers=self._headers(),
            timeout=30,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"resolve wiki node failed: {d.get('msg')} | {d}")
        self.spreadsheet_token = d["data"]["node"]["obj_token"]
        return self.spreadsheet_token

    def _find_sheet_id(self):
        if self.sheet_id:
            return self.sheet_id
        if not self.spreadsheet_token:
            self.resolve()
        r = requests.get(
            f"{BASE}/sheets/v3/spreadsheets/{self.spreadsheet_token}/sheets/query",
            headers=self._headers(),
            timeout=30,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"list sheets failed: {d.get('msg')} | {d}")
        sheets = d["data"]["sheets"]
        for s in sheets:
            if s.get("title") == self.sheet_title:
                self.sheet_id = s["sheet_id"]
                return self.sheet_id
        self.sheet_id = sheets[0]["sheet_id"]
        return self.sheet_id

    # ---------- read/write ----------
    def read_range(self, rng):
        """Read a range like "A1:D36" (sheetId is auto-prepended)."""
        if not self.sheet_id:
            self._find_sheet_id()
        url = f"{BASE}/sheets/v2/spreadsheets/{self.spreadsheet_token}/values/{self.sheet_id}!{rng}"
        r = requests.get(url, headers=self._headers(), timeout=30)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"read {rng} failed: {d.get('msg')} | {d}")
        values = d["data"].get("valueRange", {}).get("values", [])
        # flatten rich-text cells into plain strings
        return [[flatten_cell(c) for c in row] for row in values]

    def write_range(self, rng, values):
        """Write a single range. values is a list of rows, each a list of cells.

        Ensures range always has start:end format (e.g. D1 -> D1:D1).
        """
        if not self.sheet_id:
            self._find_sheet_id()
        # Ensure range has colon format: "D1" -> "D1:D1"
        if ":" not in rng:
            rng = f"{rng}:{rng}"
        body = {"valueRange": {"range": f"{self.sheet_id}!{rng}", "values": values}}
        r = requests.put(
            f"{BASE}/sheets/v2/spreadsheets/{self.spreadsheet_token}/values",
            headers=self._headers(),
            json=body,
            timeout=60,
        )
        return r.json()

    # ---------- high-level helpers ----------
    def read_table(self):
        """Read A:D and return list of dicts (skips header & non-link rows)."""
        rows = self.read_range(f"A1:D{self.max_rows}")
        out = []
        for i, row in enumerate(rows, start=1):
            g = lambda idx: (row[idx] if idx < len(row) else "") or ""
            url = g(0).strip()
            if not url:
                continue
            if not URL_RE.search(url):
                continue  # skip header / notes
            out.append({
                "row": i,
                "url": url,
                "original": g(1).strip(),
                "translation": g(2).strip(),
                "video_id": g(3).strip(),
            })
        return out

    def collect_pending(self):
        """Rows that still need extraction or translation.

        No rows are permanently skipped — every video gets retried each run
        until it succeeds. Previously failed markers are treated as pending.
        """
        pending = []
        for item in self.read_table():
            orig = item["original"]
            is_failed = (orig in FAILED_MARKERS) or (not orig)
            if is_failed or not item["translation"]:
                pending.append(item)
        return pending

    def write_back(self, results):
        """Write video_id / original / translation back as individual cell PUTs.

        results: list of dicts with 'row', 'video_id', 'original_text',
        'translated_text'. Only non-empty values are written; existing good
        content is never overwritten. Each cell is written individually to
        avoid gaps overwriting existing content with empty strings.

        Returns a status dict. If the app lacks write permission (HTTP 403),
        returns {'code': 403, ...} so the caller can fall back to CSV-only.
        """
        columns = {"B": "original_text", "C": "translated_text", "D": "video_id"}
        total = 0
        for r in results:
            row = r.get("row")
            if not row:
                continue
            for col, key in columns.items():
                val = (r.get(key) or "").strip()
                if not val:
                    continue
                rng = f"{col}{row}:{col}{row}"
                resp = self.write_range(rng, [[val]])
                rc = resp.get("code")
                if rc not in (0, None):
                    msg = str(resp.get("msg", ""))
                    if rc == 403 or "forbidden" in msg.lower() or "permission" in msg.lower():
                        return {"code": 403,
                                "msg": "app lacks write permission to the sheet",
                                "detail": resp}
                    return {"code": rc, "msg": msg, "detail": resp}
                total += 1
        return {"code": 0, "msg": f"wrote {total} cells"}


def from_env():
    """Build a FeishuClient from environment variables, or None if not set."""
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    wiki_token = os.environ.get("FEISHU_WIKI_TOKEN")
    if not (app_id and app_secret and wiki_token):
        return None
    sheet_title = os.environ.get("FEISHU_SHEET_TITLE", "脚本读取")
    return FeishuClient(app_id, app_secret, wiki_token, sheet_title=sheet_title)


if __name__ == "__main__":
    c = from_env()
    if not c:
        print("FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_WIKI_TOKEN not set.")
    else:
        print("spreadsheet_token:", c.resolve())
        print("sheet_id:", c._find_sheet_id())
        rows = c.read_table()
        print(f"read {len(rows)} link rows")
        for r in rows[:5]:
            print(r)
