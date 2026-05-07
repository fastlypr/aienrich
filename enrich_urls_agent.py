#!/usr/bin/env python3
"""Agent runner: enrich article URLs with the NVIDIA + Apify pipeline.

This is the new path that lives alongside enrich_urls_codex.py. Same Google
Sheet, same Notion DB, but the enrichment work is done stage-by-stage:
  1. fetch article HTML
  2. extract person/company facts (NVIDIA NIM)
  3. build site-restricted Google queries
  4. run them via Apify
  5. rank candidates and pick the best (heuristic + NIM)
  6. verify by fetching og:meta on the chosen profile
  7. generate the category (NIM)

Output goes to results_agent.csv and Notion. The codex runner's
results.csv is left untouched so you can A/B compare.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import agent_config
import config
import notion_writer
import sheets_fetcher
import url_utils
from agent_nvidia import NvidiaClient
from agent_pipeline import enrich_url


CSV_FIELDS = [
    "url",
    "name",
    "company",
    "linkedin",
    "instagram",
    "category",
    "status",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agent-based URL enricher (NVIDIA NIM + Apify Google Search)."
    )
    parser.add_argument("--input", default="urls.txt")
    parser.add_argument("--output", default="results_agent.csv")
    parser.add_argument("--reconfigure", action="store_true")
    parser.add_argument("--no-notion", action="store_true")
    parser.add_argument("--no-sheet", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N remaining URLs (0 = all).",
    )
    return parser.parse_args()


def load_urls_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    urls: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        normalized = url_utils.normalize(line)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def ensure_csv(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()


def read_processed(path: Path) -> set[str]:
    processed: set[str] = set()
    if not path.exists() or path.stat().st_size == 0:
        return processed
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("url") and row.get("status") == "ok":
                normalized = url_utils.normalize(row["url"])
                if normalized:
                    processed.add(normalized)
    return processed


def append_row(path: Path, row: dict) -> None:
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
        handle.flush()


def main() -> int:
    args = parse_args()

    cfg = config.load()
    cfg = config.prompt_for_missing(cfg, reconfigure=args.reconfigure)
    cfg = agent_config.prompt_for_agent_keys(cfg, reconfigure=args.reconfigure)

    nvidia_key, apify_token = agent_config.resolve_keys(cfg)
    if not nvidia_key:
        print("ERROR: NVIDIA_API_KEY missing. Run with --reconfigure.", file=sys.stderr)
        return 2

    client = NvidiaClient(api_key=nvidia_key)

    use_notion = (
        not args.no_notion
        and bool(cfg.get("notion_token"))
        and bool(cfg.get("notion_db_id"))
    )
    notion_props: dict[str, str] = {}
    if use_notion:
        try:
            notion_props = notion_writer.ensure_schema(
                cfg["notion_token"], cfg["notion_db_id"]
            )
        except Exception as exc:
            print(
                f"WARN: Notion schema setup failed: {exc}; skipping Notion writes.",
                file=sys.stderr,
            )
            use_notion = False

    output_path = Path(args.output)
    ensure_csv(output_path)
    processed = read_processed(output_path)

    if not args.no_sheet and cfg.get("sheet_url"):
        try:
            print(
                f"Fetching URLs from Google Sheet (column: {cfg.get('sheet_column', 'URL')})…"
            )
            urls = sheets_fetcher.fetch_urls(
                cfg["sheet_url"], column=cfg.get("sheet_column", "URL")
            )
            print(f"Loaded {len(urls)} URL(s) from sheet.")
        except Exception as exc:
            print(
                f"WARN: sheet fetch failed ({exc}); falling back to {args.input}.",
                file=sys.stderr,
            )
            urls = load_urls_file(Path(args.input))
    else:
        urls = load_urls_file(Path(args.input))

    remaining = [u for u in urls if url_utils.normalize(u) not in processed]
    skipped = len(urls) - len(remaining)
    if skipped:
        print(f"Skipping {skipped} URL(s) already processed (smart dedup).")

    if args.limit > 0:
        remaining = remaining[: args.limit]
        print(f"Limit applied: processing first {len(remaining)} URL(s).")

    if not remaining:
        print("No new URLs to process.")
        return 0

    if not apify_token:
        print(
            "WARN: APIFY_TOKEN not set; LinkedIn/Instagram search will be skipped.",
            file=sys.stderr,
        )

    print(f"Processing {len(remaining)} URL(s) with the agent pipeline.")
    print(f"Output CSV: {output_path}")
    if use_notion:
        print(f"Notion DB: {cfg.get('notion_db_id', '')}")

    for index, url in enumerate(remaining, start=1):
        print(f"[{index}/{len(remaining)}] {url}", flush=True)
        record = enrich_url(url, client, apify_token, verbose=True)
        append_row(output_path, record)
        if use_notion:
            try:
                notion_writer.upsert(
                    cfg["notion_token"],
                    cfg["notion_db_id"],
                    record,
                    notion_props,
                )
            except Exception as exc:
                print(f"WARN: Notion write failed: {exc}", file=sys.stderr)
        if index < len(remaining) and args.delay > 0:
            time.sleep(args.delay)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
