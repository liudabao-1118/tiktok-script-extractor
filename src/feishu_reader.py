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
import time

import requests

BASE = "https://open.feishu.cn/open-apis"

FAILED_MARKERS = ("[download_failed]", "[analysis_failed]", "[unavailable]", "[提取失败]")
URL_RE = re.compile(r"https?://(www\.|vm\.|vt\.|m\.)?tiktok\.com", re.I)

# Values the analyzer can produce for the structure column (column E).
# Anything else in that cell (empty, or a legacy 达人视频/自制视频 value) means
# the row still needs (re)classification.
STRUCTURE_VALUES = frozenset(
    {"达人口播", "AI生成", "混剪", "图文", "剧情", "测评"}
)


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

    def batch_update(self, value_ranges, attempts=4):
        """Update many ranges in ONE request.

        Writing cell-by-cell (one PUT per cell) blows through Feishu's rate
        limit: a 100-row run needs ~500 requests and the API answers
        "too many request". The batch endpoint carries all of them at once.

        value_ranges: list of {"range": "<sheetId>!B2:B4", "values": [[v], ...]}
        """
        if not self.sheet_id:
            self._find_sheet_id()
        url = f"{BASE}/sheets/v2/spreadsheets/{self.spreadsheet_token}/values_batch_update"
        body = {"valueRanges": value_ranges}

        last = {}
        for attempt in range(attempts):
            r = requests.post(url, headers=self._headers(), json=body, timeout=60)
            try:
                d = r.json()
            except ValueError:
                d = {"code": -1, "msg": f"non-JSON response ({r.status_code})"}
            if d.get("code") == 0:
                return d
            last = d
            message = str(d.get("msg") or "").lower()
            if "too many" in message or "limit" in message or r.status_code == 429:
                delay = 1.5 * (attempt + 1)
                print(f"  Feishu rate limited, retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            return d
        return last

    # ---------- high-level helpers ----------
    def read_table(self):
        """Read A:G and return list of dicts (skips header & non-link rows).

        Column layout in the sheet:
            A=视频链接  B=脚本提取原文  C=译文  D=视频ID
            E=视频结构(structure)  F=国家  G=素材分类2  H=Shop Name

        Column E holds the structure label (达人口播 / AI生成 / 混剪 / 图文 /
        剧情 / 测评), which is what the user asked to see there.
        """
        rows = self.read_range(f"A1:F{self.max_rows}")
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
                "video_structure": g(4).strip(),
                "country": g(5).strip(),
            })
        return out

    def collect_pending(self):
        """Rows that still need extraction, translation or analysis.

        No rows are permanently skipped — every video gets retried each run
        until it succeeds. Previously failed markers are treated as pending.
        Rows that already have a translation but no structure label yet (or a
        stale one) are picked up too, so column E gets (re)filled without
        re-downloading the video.
        """
        rows = self.read_table()
        print(f"  [debug] read_table returned {len(rows)} link rows")
        if rows:
            print(f"  [debug] first row: {rows[0]}")
            print(f"  [debug] last row:  {rows[-1]}")
        else:
            # Print what we actually read to diagnose empty results
            try:
                raw = self.read_range(f"A1:F{min(self.max_rows, 10)}")
                print(f"  [debug] A1:F10 raw read: {raw}")
            except Exception as e:
                print(f"  [debug] A1:F10 read failed: {e}")
        pending = []
        for item in rows:
            orig = item["original"]
            is_failed = (orig in FAILED_MARKERS) or (not orig)
            needs_extract = is_failed or not item["translation"]
            needs_analysis = (
                bool(item["translation"])
                and item["video_structure"] not in STRUCTURE_VALUES
            )
            if needs_extract or needs_analysis:
                pending.append(item)
        return pending

    def write_back(self, results, batch_size=25):
        """Write video_id / original / translation / video_type / video_structure
        back to the sheet using batched range updates.

        results: list of dicts with 'row', 'video_id', 'original_text',
        'translated_text', 'video_type', 'video_structure'.

        Only non-empty values are written, so existing content is never
        overwritten with blanks. Values are grouped per column into contiguous
        row runs (a hole in the middle starts a new range) and pushed through
        the batch endpoint -- one request per 25 ranges instead of one request
        per cell, which is what kept tripping Feishu's rate limit.

        Returns a status dict. If the app lacks write permission (403), returns
        {'code': 403, ...} so the caller can fall back to CSV-only.
        """
        # E carries the structure label (what the user wants to see there).
        # F is the user's 国家 column and is never written by us.
        columns = {"B": "original_text", "C": "translated_text",
                   "D": "video_id", "E": "video_structure"}

        # column -> {row: value}
        by_column = {col: {} for col in columns}
        for r in results:
            row = r.get("row")
            if not row:
                continue
            for col, key in columns.items():
                val = (r.get(key) or "").strip()
                if val:
                    by_column[col][row] = val

        # Collapse each column into contiguous row runs
        runs = []  # (col, start_row, end_row, mapping)
        for col, mapping in by_column.items():
            if not mapping:
                continue
            rows = sorted(mapping)
            start = prev = rows[0]
            for row in rows[1:]:
                if row == prev + 1:
                    prev = row
                    continue
                runs.append((col, start, prev, mapping))
                start = prev = row
            runs.append((col, start, prev, mapping))

        if not runs:
            return {"code": 0, "msg": "wrote 0 cells"}

        if not self.sheet_id:
            self._find_sheet_id()

        total = 0
        for i in range(0, len(runs), batch_size):
            chunk = runs[i : i + batch_size]
            value_ranges = [
                {
                    "range": f"{self.sheet_id}!{col}{start}:{col}{end}",
                    "values": [[mapping[row]] for row in range(start, end + 1)],
                }
                for col, start, end, mapping in chunk
            ]

            resp = self.batch_update(value_ranges)
            rc = resp.get("code")
            if rc not in (0, None):
                msg = str(resp.get("msg", ""))
                if rc == 403 or "forbidden" in msg.lower() or "permission" in msg.lower():
                    return {"code": 403,
                            "msg": "app lacks write permission to the sheet",
                            "detail": resp}
                return {"code": rc, "msg": msg, "detail": resp}

            total += sum(len(vr["values"]) for vr in value_ranges)

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
