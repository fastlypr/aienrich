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


INSTRUCTIONS = """Extract the main person featured in the article from the provided URL.

For that person, return:

- Full name (exact spelling from the article)
- Company or organization they are associated with
- LinkedIn profile URL
- Instagram profile URL

Use intelligent matching to ensure accuracy:

- Combine full name + company/organization
- Cross-check job title, location, industry, or achievements
- Prefer verified or highly relevant profiles
- Avoid profiles with mismatched details (wrong company, different person with same name, etc.)

If multiple profiles exist, return the most likely correct one based on strongest relevance.

If no reliable match is found, return "Not found" instead of guessing.

Then, based on the person's LinkedIn profile, generate their professional role or industry as a single phrase of 1-2 words only. The output must be lowercase and singular.

If unable to determine the role or industry clearly, return: public figure

Rules for the role/category:

- Output exactly one phrase containing 1 or 2 words only
- Output only the phrase, with no punctuation, special characters, or extra words
- Do not add sentences, explanations, names, locations, or descriptions
- If multiple roles are present, choose the primary or most relevant one
- If unclear or no role found, output: public figure

If you cannot determine the person's name or company from the article, use "Not found" for that field.

Return the final output in this exact JSON format:

{
  "name": "",
  "company": "",
  "linkedin": "",
  "instagram": "",
  "category": ""
}

Examples of valid categories:
real estate expert
fitness coach
tech entrepreneur
entrepreneur
music artist
business leader
attorney
financial advisor
motivational speaker
healthcare professional
film director
content creator
life coach
digital marketer
software engineer
fashion designer
public figure

Output rules:
- Return only valid JSON. No markdown fences. No commentary.
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
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
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
                processed.add(url)
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

    remaining = [url for url in urls if url not in processed]
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
