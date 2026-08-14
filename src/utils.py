import re
import csv
import json
import os
from datetime import datetime


def extract_video_id(url):
    """Extract video ID from a TikTok URL."""
    patterns = [
        r'/video/(\d+)',
        r'/v/(\d+)',
        r'vt\.tiktok\.com/(\w+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def extract_author(url):
    """Extract author username from a TikTok URL."""
    match = re.search(r'@([\w.]+)', url)
    if match:
        return '@' + match.group(1)
    return ''


def find_url_column(fieldnames):
    """Find the column that contains TikTok URLs."""
    possible_names = [
        'url', 'link', 'tiktok_url', 'tiktok_link',
        'video_url', 'video_link',
        '链接', '视频链接', 'tiktok链接', 'TikTok链接',
        '网址', '地址',
    ]
    lower_names = [name.lower() for name in fieldnames]
    for candidate in possible_names:
        for i, name in enumerate(lower_names):
            if name == candidate.lower():
                return fieldnames[i]
    for i, name in enumerate(lower_names):
        if 'tiktok' in name or 'url' in name or 'link' in name or '链接' in name:
            return fieldnames[i]
    return fieldnames[0] if fieldnames else None


def load_processed_ids(output_csv):
    """Load already processed video IDs and URLs from the output CSV."""
    processed_ids = set()
    processed_urls = set()
    if os.path.exists(output_csv):
        with open(output_csv, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                vid = row.get('video_id', '')
                if vid:
                    processed_ids.add(vid)
                url = row.get('url', '')
                if url:
                    processed_urls.add(url.strip())
    return processed_ids, processed_urls


def append_results_to_csv(results, output_csv):
    """Append new results to the CSV file."""
    fieldnames = [
        'video_id', 'url', 'author', 'description',
        'original_text', 'translated_text', 'language',
        'duration', 'video_type', 'video_structure',
        'extracted_at', 'status', 'error_message'
    ]

    file_exists = os.path.exists(output_csv)
    with open(output_csv, 'a', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        if not file_exists or os.path.getsize(output_csv) == 0:
            writer.writeheader()
        for row in results:
            writer.writerow(row)


def save_results_json(results, output_json):
    """Save all results (existing + new) to a JSON file."""
    all_results = []
    if os.path.exists(output_json):
        with open(output_json, 'r', encoding='utf-8') as f:
            try:
                all_results = json.load(f)
            except json.JSONDecodeError:
                all_results = []

    existing_ids = {r.get('video_id') for r in all_results}
    for row in results:
        if row.get('video_id') in existing_ids:
            for i, r in enumerate(all_results):
                if r.get('video_id') == row.get('video_id'):
                    all_results[i] = row
                    break
        else:
            all_results.append(row)

    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)


def read_input_links(input_csv):
    """Read TikTok links from the input CSV file."""
    links = []
    if not os.path.exists(input_csv):
        print(f"Input file not found: {input_csv}")
        return links, None

    with open(input_csv, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        url_col = find_url_column(fieldnames) if fieldnames else None
        if not url_col:
            print("Could not find a URL column in the input CSV.")
            return links, None

        print(f"Using column '{url_col}' as the TikTok URL column.")
        for row in reader:
            url = (row.get(url_col) or '').strip()
            if url:
                links.append({'url': url, 'raw_row': row})

    return links, url_col
