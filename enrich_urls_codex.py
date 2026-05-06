#!/usr/bin/env python3
"""Enrich URLs by sending them to Codex one by one.

This runner uses the local `codex` CLI, not the OpenAI SDK. It starts one Codex
exec session for the first URL, then resumes that same session for later URLs.
Each final Codex response is parsed as JSON and appended to a CSV immediately.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import config
import notion_writer
import sheets_fetcher
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
    "codex_session_id",
]


PROMPT_RULES = """# Profile Enricher

This skill takes article URLs and returns, for each one:

1. The person's full name
2. Their company or organization
3. The LinkedIn profile URL of the main person featured
4. The Instagram profile URL of the same person
5. A 1 to 2 word professional category in lowercase

## Workflow per URL

### Step 1: Identify the main person

Fetch the article. The main person is the subject of the article, typically:

1. The interviewee in a Q&A or feature
2. The founder/executive being profiled
3. The named subject in the headline

Ignore quoted experts, journalists, photographers, and supporting names. If multiple people are co-featured equally, pick the one named first in the headline or byline subject.

Extract:

1. Full name (exact spelling from the article)
2. Company or organization
3. Job title
4. Location (if mentioned)
5. Industry or field
6. Notable achievements mentioned

If you cannot identify a clear main person, output the "not found" JSON for this URL.

### Step 2: Find LinkedIn

Search for the person using combinations like:

1. `"Full Name" "Company" site:linkedin.com/in`
2. `"Full Name" "Job Title" site:linkedin.com/in`
3. `"Full Name" "City" site:linkedin.com/in`

Match criteria, in priority order:

1. Company name on LinkedIn matches the article
2. Job title aligns with article
3. Location matches
4. Industry aligns

Reject a candidate if any of the following is true:

1. Different company with no plausible career overlap
2. Different country/region with no relocation evidence
3. Different industry entirely
4. Same name but different person (verify with at least one corroborating detail)

If no candidate clears the bar, return `"Not found"` for the LinkedIn field. Do not guess.

### Step 3: Find Instagram

Search using:

1. `"Full Name" "Company" site:instagram.com`
2. `"Full Name" site:instagram.com` combined with industry or city
3. Check the LinkedIn profile (if found) for a linked Instagram handle, which is the strongest signal

Match criteria:

1. Bio mentions company, role, or industry from the article
2. Verified badge is a strong positive signal
3. Photos or content match the person's public image
4. Handle is plausibly tied to the name

Reject if:

1. Private account with no bio matching the person
2. Fan accounts, parody accounts, or namesakes
3. Bio describes an unrelated profession or location

If no reliable match, return `"Not found"`. Do not guess.

### Step 4: Generate the category

Use the LinkedIn profile (preferred) or, if LinkedIn was not found, the article itself to determine the role.

Rules:

1. Output exactly one phrase of 1 or 2 words
2. Lowercase only
3. Singular only (write "entrepreneur" not "entrepreneurs")
4. No punctuation, no quotes, no extra words
5. Do not include the person's name, location, or company
6. If multiple roles exist, pick the primary one (the one the article centers on)
7. If unclear, output `public figure`

Reference categories (not exhaustive, use whichever fits best):

real estate expert, fitness coach, tech entrepreneur, entrepreneur, music artist, business leader, attorney, financial advisor, motivational speaker, healthcare professional, film director, content creator, life coach, digital marketer, software engineer, fashion designer, public figure

You may use other 1 to 2 word phrases that better fit the person, as long as they follow the rules above.

### Step 5: Output

Return strict JSON in this exact shape:

```json
{ "name": "", "company": "", "linkedin": "", "instagram": "", "category": "" }
```

Use the literal string `"Not found"` (with capital N) for the name, company, linkedin, and instagram fields when no reliable match exists. The category field should always be filled, defaulting to `public figure` if the role cannot be determined.

## Anti-hallucination rules

1. Never fabricate a LinkedIn or Instagram URL. If you have not actually verified the profile exists and matches, return "Not found".
2. Do not assume a handle from the person's name. Always verify by visiting or searching.
3. If search results are ambiguous between two people with the same name, return "Not found" for that field unless you have at least two corroborating details (company AND role, or company AND location).
4. Do not output any commentary, reasoning, or extra text alongside the JSON. The JSON line is the entire output for that URL.

## Edge cases

1. Article paywalled or unfetchable: try cached/archive versions; if still no access, output `{ "name": "Not found", "company": "Not found", "linkedin": "Not found", "instagram": "Not found", "category": "public figure" }`.
2. Article is about a company, not a person: identify the founder or CEO if clearly the focus, otherwise treat as no main person and output "Not found" for name/company/linkedin/instagram and "public figure" for category.
3. Article features multiple people equally: pick the one named first in the headline.
4. Person has no LinkedIn (e.g., celebrity, athlete): return "Not found" for LinkedIn, still try Instagram.
5. Person is deceased: still attempt to find their archived profiles if they exist.

## Output format rules

- Return only valid JSON.
- No markdown fences around the JSON.
- No commentary, reasoning, or explanation alongside the JSON.
- Do not write files.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use Codex CLI to enrich article URLs and append results to CSV."
    )
    parser.add_argument("--input", default="urls.txt")
    parser.add_argument("--output", default="results.csv")
    parser.add_argument("--model", default="", help="Optional Codex model override.")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--batch-delay", type=float, default=5.0)
    parser.add_argument(
        "--session-file",
        default=".codex_enrich_session",
        help="File used to store the Codex session id between runs.",
    )
    parser.add_argument(
        "--fresh-session",
        action="store_true",
        help="Start a new Codex session instead of resuming the stored one.",
    )
    parser.add_argument(
        "--no-search",
        action="store_true",
        help="Do not pass --search to Codex. Not recommended for finding profiles.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Seconds to wait for each Codex URL run.",
    )
    parser.add_argument(
        "--rotate-after",
        type=int,
        default=50,
        help="Start a fresh Codex session after this many URLs in the current session. 0 to disable.",
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Re-prompt for sheet URL, Notion token, and Notion DB id.",
    )
    parser.add_argument(
        "--no-notion",
        action="store_true",
        help="Skip writing results to Notion. CSV is always written.",
    )
    parser.add_argument(
        "--no-sheet",
        action="store_true",
        help="Ignore the saved Google Sheet and read from --input instead.",
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
        csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()


def read_existing(path: Path) -> set[str]:
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


def append_result(path: Path, row: dict[str, str]) -> None:
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
        handle.flush()


def extract_json(text: str) -> dict[str, str]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
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
        raise ValueError("Codex output was not a JSON object")

    category = str(data.get("category") or "public figure").strip().lower()
    category = re.sub(r"[^a-z ]+", "", category)
    words = category.split()
    if not words or len(words) > 2:
        category = "public figure"

    return {
        "name": str(data.get("name") or "Not found").strip(),
        "company": str(data.get("company") or "Not found").strip(),
        "linkedin": str(data.get("linkedin") or "Not found").strip(),
        "instagram": str(data.get("instagram") or "Not found").strip(),
        "category": category,
    }


def parse_session_id(jsonl: str) -> str | None:
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = json.dumps(event)
        match = re.search(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            text,
        )
        if match:
            return match.group(0)
    return None


def make_prompt(url: str, *, include_rules: bool) -> str:
    if include_rules:
        return f"""{PROMPT_RULES}

Process this URL now:
{url}
"""
    return f"""Process this URL now:
{url}
"""


def run_codex(
    url: str,
    *,
    cwd: Path,
    model: str,
    session_id: str | None,
    use_search: bool,
    timeout: int,
    include_rules: bool,
) -> tuple[dict[str, str], str | None]:
    with tempfile.NamedTemporaryFile("r+", encoding="utf-8", delete=False) as tmp:
        output_path = Path(tmp.name)

    prompt = make_prompt(url, include_rules=include_rules)
    command = ["codex"]
    if use_search:
        command.append("--search")

    command.append("exec")
    if session_id:
        command.extend(["resume", "--skip-git-repo-check"])
    else:
        command.append("--skip-git-repo-check")

    if model:
        command.extend(["--model", model])

    command.extend(["--json", "-o", str(output_path)])
    if session_id:
        command.extend([session_id, "-"])
    else:
        command.append("-")

    try:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(cwd),
            timeout=timeout,
            check=False,
        )

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(message or f"codex exited with code {result.returncode}")

        final_text = output_path.read_text(encoding="utf-8")
        parsed = extract_json(final_text)
        found_session_id = session_id or parse_session_id(result.stdout)
        return parsed, found_session_id
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    args = parse_args()
    cwd = Path.cwd()
    input_path = Path(args.input)
    output_path = Path(args.output)
    session_path = Path(args.session_file)

    cfg = config.load()
    cfg = config.prompt_for_missing(cfg, reconfigure=args.reconfigure)

    use_notion = not args.no_notion and bool(cfg.get("notion_token"))
    if use_notion and not cfg.get("notion_db_id") and cfg.get("notion_parent_page_id"):
        print("Creating Notion database under parent page...")
        try:
            db_id = notion_writer.create_database(
                cfg["notion_token"], cfg["notion_parent_page_id"]
            )
            cfg["notion_db_id"] = db_id
            cfg.pop("notion_parent_page_id", None)
            config.save(cfg)
            print(f"Created Notion database: {db_id}")
        except Exception as exc:
            print(f"WARN: could not create Notion database: {exc}", file=sys.stderr)
            use_notion = False

    if use_notion and not cfg.get("notion_db_id"):
        print("WARN: no Notion database id; skipping Notion writes.", file=sys.stderr)
        use_notion = False

    notion_props: dict[str, str] = {}
    if use_notion:
        try:
            notion_props = notion_writer.ensure_schema(
                cfg["notion_token"], cfg["notion_db_id"]
            )
        except Exception as exc:
            print(
                f"WARN: could not prepare Notion database schema: {exc}; "
                "skipping Notion writes.",
                file=sys.stderr,
            )
            use_notion = False

    if not args.no_sheet and cfg.get("sheet_url"):
        try:
            print(f"Fetching URLs from Google Sheet (column: {cfg.get('sheet_column', 'URL')})...")
            urls = sheets_fetcher.fetch_urls(
                cfg["sheet_url"], column=cfg.get("sheet_column", "URL")
            )
            print(f"Loaded {len(urls)} URL(s) from sheet.")
        except Exception as exc:
            print(f"WARN: sheet fetch failed ({exc}); falling back to {input_path}.", file=sys.stderr)
            urls = load_urls(input_path)
    else:
        urls = load_urls(input_path)

    ensure_csv(output_path)
    processed = read_existing(output_path)
    remaining = [url for url in urls if url_utils.normalize(url) not in processed]
    skipped = len(urls) - len(remaining)
    if skipped:
        print(f"Skipping {skipped} URL(s) already processed (smart dedup).")

    if not remaining:
        print("No new URLs to process.")
        return 0

    session_id = None
    if not args.fresh_session and session_path.exists():
        session_id = session_path.read_text(encoding="utf-8").strip() or None

    print(f"Processing {len(remaining)} URL(s) with Codex. Output: {output_path}")
    if session_id:
        print(f"Resuming Codex session: {session_id}")

    urls_in_session = 0
    for index, url in enumerate(remaining, start=1):
        print(f"[{index}/{len(remaining)}] {url}")
        if (
            args.rotate_after > 0
            and session_id is not None
            and urls_in_session >= args.rotate_after
        ):
            print(f"Rotating to a fresh Codex session after {urls_in_session} URLs.")
            session_id = None
            urls_in_session = 0
        include_rules = session_id is None
        try:
            try:
                data, session_id = run_codex(
                    url,
                    cwd=cwd,
                    model=args.model,
                    session_id=session_id,
                    use_search=not args.no_search,
                    timeout=args.timeout,
                    include_rules=include_rules,
                )
            except (json.JSONDecodeError, ValueError) as parse_exc:
                if include_rules:
                    raise
                print(
                    f"Output did not parse ({parse_exc}); re-sending rules and retrying.",
                    file=sys.stderr,
                )
                data, session_id = run_codex(
                    url,
                    cwd=cwd,
                    model=args.model,
                    session_id=session_id,
                    use_search=not args.no_search,
                    timeout=args.timeout,
                    include_rules=True,
                )

            if session_id:
                session_path.write_text(session_id, encoding="utf-8")

            urls_in_session += 1

            row = {
                "url": url,
                "name": data["name"],
                "company": data["company"],
                "linkedin": data["linkedin"],
                "instagram": data["instagram"],
                "category": data["category"],
                "status": "ok",
                "error": "",
                "codex_session_id": session_id or "",
            }
            append_result(output_path, row)
            if use_notion:
                try:
                    notion_writer.upsert(
                        cfg["notion_token"], cfg["notion_db_id"], row, notion_props
                    )
                except Exception as notion_exc:
                    print(f"WARN: Notion write failed: {notion_exc}", file=sys.stderr)
        except Exception as exc:
            row = {
                "url": url,
                "name": "Not found",
                "company": "Not found",
                "linkedin": "Not found",
                "instagram": "Not found",
                "category": "public figure",
                "status": "error",
                "error": str(exc),
                "codex_session_id": session_id or "",
            }
            append_result(output_path, row)
            if use_notion:
                try:
                    notion_writer.upsert(
                        cfg["notion_token"], cfg["notion_db_id"], row, notion_props
                    )
                except Exception as notion_exc:
                    print(f"WARN: Notion write failed: {notion_exc}", file=sys.stderr)
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
