#!/usr/bin/env python3
"""Exa runner: enrich article URLs with the NVIDIA + Exa.ai pipeline.

A standalone alternative to the Apify agent runner. Same Google Sheet, same
Notion DB, but the LinkedIn/website search is done by Exa.ai and the matching
is done by the LLM:

  1. fetch article HTML
  2. extract person facts + category   (NVIDIA NIM, one call)
  3. build one Exa query
  4. Exa search (10 results)
  5. match LinkedIn + website          (NVIDIA NIM, one call)
  6. write results_exa.csv + Notion

The Apify runner (enrich_urls_agent.py / results_agent.csv) is left untouched.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import agent_config
import config
import notion_writer_exa
import sheets_fetcher
import url_utils
from agent_nvidia import NvidiaClient
from exa_pipeline import enrich_url_exa


CSV_FIELDS = [
    "url",
    "name",
    "company",
    "linkedin",
    "website",
    "category",
    "status",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exa-based URL enricher (NVIDIA NIM + Exa.ai search)."
    )
    parser.add_argument("--input", default="urls.txt")
    parser.add_argument("--output", default="results_exa.csv")
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


def _ask(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{agent_config._mask(default) if secret else default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        raise SystemExit(
            "ERROR: stdin closed during config prompt. Run interactively first "
            "or set NVIDIA_API_KEY / EXA_API_KEY env vars."
        )
    return value or default


def resolve_exa_keys(cfg: dict[str, str], *, reconfigure: bool) -> tuple[str, str]:
    """Return (nvidia_key, exa_key), prompting for whatever is missing.

    Kept inside this runner so the shared agent_config.py is not modified and
    the Exa workflow never prompts for an Apify token.
    """
    if reconfigure or not (os.getenv("NVIDIA_API_KEY") or cfg.get("nvidia_api_key")):
        cfg["nvidia_api_key"] = _ask(
            "NVIDIA API key (from build.nvidia.com)",
            cfg.get("nvidia_api_key", ""),
            secret=True,
        )
    if reconfigure or not (os.getenv("EXA_API_KEY") or cfg.get("exa_api_key")):
        cfg["exa_api_key"] = _ask(
            "Exa API key (from exa.ai — leave blank to skip search)",
            cfg.get("exa_api_key", ""),
            secret=True,
        )
    config.save(cfg)

    nvidia = os.getenv("NVIDIA_API_KEY") or cfg.get("nvidia_api_key", "")
    exa = os.getenv("EXA_API_KEY") or cfg.get("exa_api_key", "")
    return nvidia, exa


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

    agent_config.load_dotenv()

    cfg = config.load()
    # Only prompt for Sheet/Notion settings when they're actually needed.
    # A fully isolated run (--no-sheet --no-notion) skips those prompts so it
    # works non-interactively.
    if not (args.no_sheet and args.no_notion):
        cfg = config.prompt_for_missing(cfg, reconfigure=args.reconfigure)
    nvidia_key, exa_key = resolve_exa_keys(cfg, reconfigure=args.reconfigure)
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
            notion_props = notion_writer_exa.ensure_schema(
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

    if not exa_key:
        print(
            "WARN: EXA_API_KEY not set; LinkedIn/website search will be skipped.",
            file=sys.stderr,
        )

    print(f"Processing {len(remaining)} URL(s) with the Exa pipeline.")
    print(f"Output CSV: {output_path}")
    if use_notion:
        print(f"Notion DB: {cfg.get('notion_db_id', '')}")

    for index, url in enumerate(remaining, start=1):
        print(f"[{index}/{len(remaining)}] {url}", flush=True)
        record = enrich_url_exa(url, client, exa_key, verbose=True)
        append_row(output_path, record)
        if use_notion:
            try:
                notion_writer_exa.upsert(
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
