#!/usr/bin/env python3
"""
TikTok Script Extractor - Main Entry Point

Two input modes:
  1. Feishu direct (preferred, no CSV export needed)
     Set GitHub Secrets: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_WIKI_TOKEN
     The automation reads TikTok links from the Feishu spreadsheet and writes
     transcripts/translations straight back -- no manual export required.
  2. CSV fallback
     Reads input/tiktok_links.csv, writes output/results.csv + results.json.

In both modes results are also saved locally (output/) as a backup / history.
"""

import sys
import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta

# Add src directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    read_input_links,
    append_results_to_csv,
    save_results_json,
)
from extractor import TikTokExtractor
from translator import Translator
from feishu_reader import from_env

# Configuration
INPUT_CSV = os.path.join(os.path.dirname(__file__), "..", "input", "tiktok_links.csv")
OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "..", "output", "results.csv")
OUTPUT_JSON = os.path.join(os.path.dirname(__file__), "..", "output", "results.json")
MAX_VIDEOS_PER_RUN = 50  # Limit per run to avoid GitHub timeout


def send_feishu_notification(summary):
    """Send a notification to Feishu bot webhook after each run."""
    webhook = os.environ.get("FEISHU_BOT_WEBHOOK", "")
    if not webhook:
        return

    # Beijing time (UTC+8)
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    time_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")

    status_emoji = "✅" if summary.get("failed", 0) == 0 else "⚠️"
    lines = [
        f"{status_emoji} TikTok脚本提取通知",
        f"时间：{time_str}",
        f"待处理：{summary.get('pending', 0)} 行",
        f"已处理：{summary.get('processed', 0)} 行",
        f"成功：{summary.get('succeeded', 0)} 行",
        f"失败：{summary.get('failed', 0)} 行",
    ]
    if summary.get("skipped", 0) > 0:
        lines.append(f"跳过(暗帖/已删除)：{summary['skipped']} 行")
    if summary.get("written_cells"):
        lines.append(f"写回飞书：{summary['written_cells']} 个单元格")
    if summary.get("failed", 0) > 0:
        lines.append("失败行会在下次运行时自动重试")

    text = "\n".join(lines)
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")

    try:
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  Feishu notification sent: {resp.status}")
    except Exception as e:
        print(f"  Feishu notification failed: {e}")


def process_one(extractor, translator, url):
    """Extract transcript + translate for a single URL. Returns result dict."""
    result = extractor.extract(url)
    # For permanently unavailable videos (dark posts, deleted), write the
    # same marker to both B and C columns so collect_pending skips them.
    if result.get("status") in ("dark_post", "deleted"):
        result["translated_text"] = result["original_text"]
    elif result["original_text"]:
        result["translated_text"] = translator.translate(result["original_text"]) or ""
    else:
        result["translated_text"] = ""
    result["extracted_at"] = datetime.now().isoformat()
    return result


def run_feishu_mode(feishu, extractor, translator):
    """Read links from Feishu, process pending rows, write back directly."""
    print("Mode: Feishu direct read/write")
    pending = feishu.collect_pending()
    print(f"Pending rows (need extract/translate): {len(pending)}")

    summary = {"pending": len(pending), "processed": 0, "succeeded": 0, "failed": 0, "skipped": 0, "written_cells": 0}

    if not pending:
        print("Nothing to do. All rows are up to date.")
        return summary

    # Deduplicate by video_id so we don't re-download the same video for dup rows
    seen = {}  # video_id -> result
    results_to_write = []

    todo = pending[:MAX_VIDEOS_PER_RUN]
    print(f"Processing up to {len(todo)} rows this run.")
    for i, item in enumerate(todo, 1):
        url = item["url"]
        vid = item.get("video_id") or ""
        print(f"\n[{i}/{len(todo)}] row {item['row']}: {url}")

        if vid and vid in seen:
            result = dict(seen[vid])
            print("  (reuse transcript from duplicate video_id)")
        else:
            result = process_one(extractor, translator, url)
            if not result.get("video_id") and vid:
                result["video_id"] = vid  # keep existing id from sheet
            if vid:
                seen[vid] = result

        result["row"] = item["row"]
        summary["processed"] += 1
        if result.get("status") in ("dark_post", "deleted"):
            summary["skipped"] += 1
        elif result.get("original_text"):
            summary["succeeded"] += 1
        else:
            summary["failed"] += 1
        print(f"  Status: {result['status']}")
        if result["original_text"]:
            print(f"  Original: {result['original_text'][:80]}...")
        if result["translated_text"]:
            print(f"  Translated: {result['translated_text'][:80]}...")
        results_to_write.append(result)

    print("\nWriting results back to Feishu...")
    resp = feishu.write_back(results_to_write)
    print(f"  Feishu write: {resp.get('msg')}")
    # Extract cell count from response message like "wrote 18 cells"
    msg = resp.get("msg", "")
    if "wrote" in msg:
        try:
            summary["written_cells"] = int(msg.split("wrote")[1].split("cells")[0].strip())
        except Exception:
            pass

    # Local backup (non-fatal — Feishu write is the primary output)
    try:
        os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
        append_results_to_csv(results_to_write, OUTPUT_CSV)
        save_results_json(results_to_write, OUTPUT_JSON)
        print(f"  Local backup: {OUTPUT_CSV}, {OUTPUT_JSON}")
    except Exception as e:
        print(f"  Local backup skipped: {e}")

    return summary


def run_csv_mode(extractor, translator):
    """Original CSV-input mode (fallback)."""
    print("Mode: CSV input (set FEISHU_* secrets to enable direct Feishu mode)")
    links, url_col = read_input_links(INPUT_CSV)
    if not links:
        print("No TikTok links found in input. Exiting.")
        return

    print(f"Found {len(links)} links in input file.")

    # Load already processed IDs/URLs from local output
    from utils import load_processed_ids
    processed_ids, processed_urls = load_processed_ids(OUTPUT_CSV)
    print(f"Already processed: {len(processed_ids)} videos.")

    from utils import extract_video_id
    new_links = []
    for link in links:
        vid = extract_video_id(link["url"])
        if (vid and vid in processed_ids) or (link["url"].strip() in processed_urls):
            continue
        new_links.append(link)

    print(f"New videos to process: {len(new_links)}")
    if not new_links:
        print("No new videos to process. Exiting.")
        return

    if len(new_links) > MAX_VIDEOS_PER_RUN:
        print(f"Limiting to {MAX_VIDEOS_PER_RUN} videos per run (total: {len(new_links)}).")
        new_links = new_links[:MAX_VIDEOS_PER_RUN]

    results = []
    for i, link in enumerate(new_links, 1):
        print(f"\n[{i}/{len(new_links)}] Processing: {link['url']}")
        result = process_one(extractor, translator, link["url"])
        print(f"  Status: {result['status']}")
        if result["original_text"]:
            print(f"  Original: {result['original_text'][:80]}...")
        if result["translated_text"]:
            print(f"  Translated: {result['translated_text'][:80]}...")
        results.append(result)

    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    append_results_to_csv(results, OUTPUT_CSV)
    save_results_json(results, OUTPUT_JSON)
    print(f"\nSaved {len(results)} results -> {OUTPUT_CSV}, {OUTPUT_JSON}")


def main():
    print("=" * 60)
    print(f"TikTok Script Extractor - Run at {datetime.now().isoformat()}")
    print("=" * 60)

    extractor = TikTokExtractor(whisper_model="tiny")
    translator = Translator(target_lang="zh-CN")

    summary = None
    feishu = from_env()
    if feishu:
        try:
            summary = run_feishu_mode(feishu, extractor, translator)
        except Exception as e:
            print(f"Feishu mode error: {e}")
            summary = {"pending": 0, "processed": 0, "succeeded": 0, "failed": 1, "skipped": 0, "written_cells": 0, "error": str(e)}
            if os.path.exists(INPUT_CSV):
                print("Falling back to CSV mode.")
                run_csv_mode(extractor, translator)
            else:
                print("No CSV input file available. Will retry next run.")
    else:
        run_csv_mode(extractor, translator)

    # Send Feishu bot notification
    if summary:
        send_feishu_notification(summary)

    print(f"\nDone. Next run in 5 minutes.")


if __name__ == "__main__":
    main()
