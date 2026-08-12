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
from datetime import datetime

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
MAX_VIDEOS_PER_RUN = 10  # Limit per run to avoid GitHub timeout


def process_one(extractor, translator, url):
    """Extract transcript + translate for a single URL. Returns result dict."""
    result = extractor.extract(url)
    if result["original_text"]:
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

    if not pending:
        print("Nothing to do. All rows are up to date.")
        return

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
        print(f"  Status: {result['status']}")
        if result["original_text"]:
            print(f"  Original: {result['original_text'][:80]}...")
        if result["translated_text"]:
            print(f"  Translated: {result['translated_text'][:80]}...")
        results_to_write.append(result)

    print("\nWriting results back to Feishu...")
    resp = feishu.write_back(results_to_write)
    print(f"  Feishu write: {resp.get('msg')}")

    # Local backup
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    append_results_to_csv(results_to_write, OUTPUT_CSV)
    save_results_json(results_to_write, OUTPUT_JSON)
    print(f"  Local backup: {OUTPUT_CSV}, {OUTPUT_JSON}")


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

    feishu = from_env()
    if feishu:
        try:
            run_feishu_mode(feishu, extractor, translator)
        except Exception as e:
            print(f"Feishu mode error: {e}")
            print("Falling back to CSV mode.")
            run_csv_mode(extractor, translator)
    else:
        run_csv_mode(extractor, translator)

    print(f"\nDone. Next run in 30 minutes.")


if __name__ == "__main__":
    main()
