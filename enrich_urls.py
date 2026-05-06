#!/usr/bin/env python3
"""Enrich article URLs one by one and append results to CSV.

The script keeps a single OpenAI Responses API session by passing the
previous response ID into the next request. It does not resend the full
conversation history, which keeps client-side token usage lower while still
letting the API preserve state.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

import url_utils


CSV_FIELDS = [
    "url",
    "name",
    "company",
    "linkedin",
    "instagram",
    "category",
    "status",
    "error",
    "response_id",
]


INSTRUCTIONS = """# Profile Enricher

For each article URL, identify the main person featured and return their full
name, company, LinkedIn profile URL, Instagram profile URL, and a 1-2 word
professional category.

## Step 1: Identify the main person

The main person is the subject of the article — interviewee, founder/executive
being profiled, or the named subject in the headline. Ignore quoted experts,
journalists, photographers, and supporting names. If multiple people are
co-featured equally, pick the one named first in the headline.

Extract: full name (exact spelling), company/organization, job title, location
(if mentioned), industry/field, notable achievements.

If you cannot identify a clear main person, return "Not found" for name,
company, linkedin, and instagram.

## Step 2: Find LinkedIn

Search using combinations like:
- `"Full Name" "Company" site:linkedin.com/in`
- `"Full Name" "Job Title" site:linkedin.com/in`
- `"Full Name" "City" site:linkedin.com/in`

Match criteria (priority order): company match, job title alignment, location
match, industry alignment.

Reject if: different company without career overlap, different country without
relocation evidence, different industry entirely, or same name but different
person (verify with at least one corroborating detail).

If no candidate clears the bar, return "Not found". Do not guess.

## Step 3: Find Instagram

Search using:
- `"Full Name" "Company" site:instagram.com`
- `"Full Name" site:instagram.com` with industry or city
- Check the LinkedIn profile for a linked Instagram handle (strongest signal)

Accept if: bio mentions company/role/industry from the article, verified badge,
content matches the person's public image, handle is plausibly tied to the
name.

Reject if: private account without matching bio, fan/parody/namesake account,
bio describes unrelated profession or location.

If no reliable match, return "Not found".

## Step 4: Category

Use the LinkedIn profile (preferred) or the article to determine the role.
Output exactly one lowercase, singular phrase of 1 or 2 words. No punctuation,
quotes, names, locations, or descriptions. If multiple roles, pick the primary
one. If unclear, output `public figure`.

Reference categories (not exhaustive): real estate expert, fitness coach, tech
entrepreneur, entrepreneur, music artist, business leader, attorney, financial
advisor, motivational speaker, healthcare professional, film director, content
creator, life coach, digital marketer, software engineer, fashion designer,
public figure.

## Anti-hallucination rules

1. Never fabricate a LinkedIn or Instagram URL. Verify before returning.
2. Do not assume a handle from the person's name.
3. If results are ambiguous between two people with the same name, return
   "Not found" unless you have at least two corroborating details (company
   AND role, or company AND location).

## Output

Return only valid JSON in this exact shape, no markdown fences, no commentary:

{
  "name": "",
  "company": "",
  "linkedin": "",
  "instagram": "",
  "category": ""
}

Use the literal string "Not found" (capital N) for any field that cannot be
reliably determined. Category always defaults to "public figure" if unclear.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich article URLs and save LinkedIn, Instagram, and category to CSV."
    )
    parser.add_argument(
        "--input",
        default="urls.txt",
        help="Text file with one URL per line. Blank lines and # comments are ignored.",
    )
    parser.add_argument(
        "--output",
        default="results.csv",
        help="CSV file to append results to.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        help="OpenAI model to use. Defaults to OPENAI_MODEL or gpt-4.1-mini.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Number of URLs to process before a longer pause.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between URLs.",
    )
    parser.add_argument(
        "--batch-delay",
        type=float,
        default=5.0,
        help="Seconds to wait after each batch.",
    )
    parser.add_argument(
        "--fresh-session",
        action="store_true",
        help="Do not continue from the last response_id found in the CSV.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=700,
        help="Maximum output tokens for each response.",
    )
    return parser.parse_args()


def load_urls(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    urls: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
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
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()


def read_existing(path: Path) -> tuple[set[str], str | None]:
    processed: set[str] = set()
    last_response_id: str | None = None

    if not path.exists() or path.stat().st_size == 0:
        return processed, last_response_id

    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            url = (row.get("url") or "").strip()
            status = (row.get("status") or "").strip()
            response_id = (row.get("response_id") or "").strip()
            if url and status == "ok":
                normalized = url_utils.normalize(url)
                if normalized:
                    processed.add(normalized)
            if response_id:
                last_response_id = response_id

    return processed, last_response_id


def append_result(path: Path, row: dict[str, str]) -> None:
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
        handle.flush()


def extract_json(text: str) -> dict[str, str]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()

    try:
        data: Any = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(stripped[start : end + 1])

    if not isinstance(data, dict):
        raise ValueError("Model output was not a JSON object")

    return {
        "name": str(data.get("name") or "Not found").strip(),
        "company": str(data.get("company") or "Not found").strip(),
        "linkedin": str(data.get("linkedin") or "Not found").strip(),
        "instagram": str(data.get("instagram") or "Not found").strip(),
        "category": str(data.get("category") or "public figure").strip().lower(),
    }


def make_input(url: str) -> str:
    return f"""Process this article URL and return only the requested JSON.

URL: {url}
"""


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2

    urls = load_urls(input_path)
    ensure_csv(output_path)
    processed, previous_response_id = read_existing(output_path)

    if args.fresh_session:
        previous_response_id = None

    remaining = [url for url in urls if url_utils.normalize(url) not in processed]
    skipped = len(urls) - len(remaining)
    if skipped:
        print(f"Skipping {skipped} URL(s) already processed (smart dedup).")
    if not remaining:
        print("No new URLs to process.")
        return 0

    client = OpenAI()

    print(f"Processing {len(remaining)} URL(s). Output: {output_path}")
    if previous_response_id:
        print(f"Continuing session from response_id: {previous_response_id}")

    for index, url in enumerate(remaining, start=1):
        print(f"[{index}/{len(remaining)}] {url}")
        response_id = ""

        try:
            kwargs: dict[str, Any] = {
                "model": args.model,
                "instructions": INSTRUCTIONS,
                "input": make_input(url),
                "max_output_tokens": args.max_output_tokens,
                "store": True,
                "tools": [{"type": "web_search_preview"}],
            }
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id

            response = client.responses.create(**kwargs)
            response_id = response.id
            previous_response_id = response.id
            data = extract_json(response.output_text)

            append_result(
                output_path,
                {
                    "url": url,
                    "name": data["name"],
                    "company": data["company"],
                    "linkedin": data["linkedin"],
                    "instagram": data["instagram"],
                    "category": data["category"],
                    "status": "ok",
                    "error": "",
                    "response_id": response_id,
                },
            )
        except Exception as exc:
            append_result(
                output_path,
                {
                    "url": url,
                    "name": "Not found",
                    "company": "Not found",
                    "linkedin": "Not found",
                    "instagram": "Not found",
                    "category": "public figure",
                    "status": "error",
                    "error": str(exc),
                    "response_id": response_id,
                },
            )
            print(f"ERROR: {exc}", file=sys.stderr)

        if index < len(remaining):
            if args.batch_size > 0 and index % args.batch_size == 0:
                time.sleep(args.batch_delay)
            else:
                time.sleep(args.delay)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
