#!/usr/bin/env python3
"""Enrich URLs with opencode.ai + a profile-finder skill.

How it works:

  1. We pull URLs from the Google Sheet (or --input file).
  2. For each URL, we shell out to `opencode run "<prompt>"`. The skill
     installed in your opencode workspace handles fetching the article,
     searching for profiles, and verifying.
  3. The skill's final message must be strict JSON in this shape:

         {
           "name":     "...",
           "company":  "...",
           "linkedin": "...",
           "instagram":"...",
           "category": "..."
         }

  4. We parse that JSON out of the opencode stdout, append a row to
     results_opencode.csv, and upsert into Notion via notion_writer.

This means the skill should NOT write to Notion itself. Its only job is
extraction + search + return JSON. Notion writes happen here, the same
way the agent and codex runners do them.

Session reuse:
  --rotate-after N  rotate to a fresh opencode session every N URLs (cap
                    growing context, default 50, 0 to disable)
  --fresh-session   force a new session for this run
  -c (auto)         the runner uses opencode's --continue for URLs 2..N
                    so the skill prompt isn't re-billed every URL
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import agent_config
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
    "elapsed_seconds",
]

SESSION_MARKER = ".opencode_session_active"

DEFAULT_PROMPT_TEMPLATE = """Use the profile-finder workflow on the article URL below. Follow every step. Do not skip the search step.

Step 1 — Identify the main person
Use webfetch to read the article body. The main person is the article's subject — usually the founder, CEO, or interviewee profiled in the headline. Ignore quoted experts, journalists, and supporting names. If multiple people are co-featured equally, pick the one named first in the headline.
Extract: full name (exact spelling), company or organization, job title or role, location if mentioned.

Step 2 — Search for LinkedIn (REQUIRED, do not skip)
Use web search. Try these queries until you find a confident match:
  - "[Full Name] [Company] linkedin"
  - "[Full Name] [Role] linkedin"
  - "[Full Name] [Location] linkedin"
Examine the top results. A confident match means the linkedin.com/in/<handle> snippet mentions the person's company, role, or location.
If no result clears the bar, return "Not found". Never invent a URL.

Step 3 — Search for Instagram (REQUIRED, do not skip)
Use web search:
  - "[Full Name] [Company] instagram"
  - "[Full Name] instagram"
Accept only instagram.com/<handle> profile URLs whose bio or content confirms the person.
If no result clears the bar, return "Not found".

Step 4 — Generate category
A 1 to 2 word lowercase phrase describing the person's primary professional role (entrepreneur, fitness coach, real estate agent, etc.). If unclear, return: public figure.

Step 5 — Output
Return ONLY the JSON object below. No commentary. No markdown fences. No "Here is" or "I found". Just the JSON, on a single line, as your entire final message.

{{"name":"","company":"","linkedin":"","instagram":"","category":""}}

Use "Not found" (capital N) for any field that cannot be reliably determined.

Article URL: {url}"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich URLs via opencode.ai + the profile-finder skill."
    )
    parser.add_argument("--input", default="urls.txt")
    parser.add_argument("--output", default="results_opencode.csv")
    parser.add_argument(
        "--model",
        default="opencode/minimax-m2.5-free",
        help="opencode model id. Run `opencode models` to list available ids.",
    )
    parser.add_argument(
        "--agent",
        default="",
        help="opencode agent name to use, if your skill is wrapped in one.",
    )
    parser.add_argument(
        "--prompt-template",
        default=DEFAULT_PROMPT_TEMPLATE,
        help="Template sent to opencode. {url} is replaced with the article URL.",
    )
    parser.add_argument("--reconfigure", action="store_true")
    parser.add_argument("--no-sheet", action="store_true")
    parser.add_argument("--no-notion", action="store_true")
    parser.add_argument("--fresh-session", action="store_true")
    parser.add_argument(
        "--rotate-after",
        type=int,
        default=10,
        help=(
            "Start a fresh opencode session every N URLs. 0 to disable, "
            "1 = fresh session every URL. With drift detection enabled "
            "(--fast-threshold), rotation usually happens earlier when "
            "the model starts skipping search steps."
        ),
    )
    parser.add_argument(
        "--fast-threshold",
        type=float,
        default=15.0,
        help=(
            "If a URL completes in fewer than this many seconds while "
            "in a continued session, force a fresh session before the "
            "next URL. A real LinkedIn+Instagram search takes 30-90s, "
            "so anything under 15s is almost certainly the model "
            "shortcutting to 'Not found' without searching. Set to 0 "
            "to disable."
        ),
    )
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-delay", type=float, default=5.0)
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Seconds to wait for each opencode run.",
    )
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


def parse_skill_output(text: str) -> dict:
    """Extract the JSON object from opencode/skill stdout.

    Tolerates surrounding TUI noise, ``` fences, and explanatory text. We
    look for the largest {...} block that parses as JSON. Raises on failure.
    """
    if not text:
        raise ValueError("opencode produced no output to parse")

    # Strip ANSI escape sequences (TUI colours/cursor moves)
    text = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", text)

    # Strip common code-fence wrappers
    fenced = re.sub(r"```(?:json)?\s*", "", text)
    fenced = fenced.replace("```", "")

    # Try whole-text parse first
    try:
        data = json.loads(fenced.strip())
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Fallback: scan for { ... } substrings, take the largest that parses
    best: dict | None = None
    best_len = 0
    for match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", fenced, re.DOTALL):
        chunk = match.group(0)
        if len(chunk) <= best_len:
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and any(
            k in data for k in ("name", "linkedin", "instagram", "category")
        ):
            best = data
            best_len = len(chunk)

    if best is None:
        raise ValueError("could not find a JSON object in opencode output")
    return best


def normalize_record(url: str, data: dict) -> dict:
    def coerce(value, default="Not found") -> str:
        if value is None:
            return default
        text = str(value).strip()
        if not text or text.lower() in ("none", "null", "n/a"):
            return default
        return text

    category = coerce(data.get("category"), default="public figure").lower()
    category = re.sub(r"[^a-z ]+", "", category).strip()
    words = category.split()
    if not words or len(words) > 2:
        category = "public figure"

    return {
        "url": url,
        "name": coerce(data.get("name")),
        "company": coerce(data.get("company")),
        "linkedin": coerce(data.get("linkedin")),
        "instagram": coerce(data.get("instagram")),
        "category": category,
        "status": "ok",
        "error": "",
    }


def run_opencode(
    url: str,
    *,
    template: str,
    model: str,
    agent: str,
    continue_session: bool,
    timeout: int,
) -> tuple[bool, str, str, float]:
    """Invoke `opencode run`. Returns (success, stdout, error_msg, seconds)."""
    cmd: list[str] = ["opencode", "run"]
    if continue_session:
        cmd.append("-c")
    if model:
        cmd.extend(["-m", model])
    if agent:
        cmd.extend(["--agent", agent])
    prompt = template.format(url=url)
    cmd.append(prompt)

    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s", time.time() - started
    except FileNotFoundError:
        return (
            False,
            "",
            "opencode binary not found on PATH.",
            time.time() - started,
        )

    elapsed = time.time() - started
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        return False, result.stdout or "", f"exit {result.returncode}: {msg[:300]}", elapsed
    return True, result.stdout or "", "", elapsed


def main() -> int:
    args = parse_args()

    agent_config.load_dotenv()

    cfg = config.load()
    cfg = config.prompt_for_missing(cfg, reconfigure=args.reconfigure)

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

    print(f"Processing {len(remaining)} URL(s) with opencode.")
    print(f"Output CSV: {output_path}")
    print(f"Model: {args.model}")
    if args.agent:
        print(f"Agent: {args.agent}")

    session_marker = Path(SESSION_MARKER)
    have_session = session_marker.exists() and not args.fresh_session
    urls_in_session = 0

    for index, url in enumerate(remaining, start=1):
        print(f"[{index}/{len(remaining)}] {url}", flush=True)

        if (
            args.rotate_after > 0
            and have_session
            and urls_in_session >= args.rotate_after
        ):
            print(
                f"  rotating to a fresh opencode session after {urls_in_session} URLs"
            )
            have_session = False
            urls_in_session = 0
            session_marker.unlink(missing_ok=True)

        success, stdout, error_msg, elapsed = run_opencode(
            url,
            template=args.prompt_template,
            model=args.model,
            agent=args.agent,
            continue_session=have_session,
            timeout=args.timeout,
        )

        if not success:
            row = {
                "url": url,
                "name": "Not found",
                "company": "Not found",
                "linkedin": "Not found",
                "instagram": "Not found",
                "category": "public figure",
                "status": "error",
                "error": error_msg,
                "elapsed_seconds": f"{elapsed:.1f}",
            }
            append_row(output_path, row)
            print(f"  ERROR ({elapsed:.1f}s): {error_msg}", file=sys.stderr, flush=True)
        else:
            try:
                parsed = parse_skill_output(stdout)
                record = normalize_record(url, parsed)
            except Exception as exc:
                row = {
                    "url": url,
                    "name": "Not found",
                    "company": "Not found",
                    "linkedin": "Not found",
                    "instagram": "Not found",
                    "category": "public figure",
                    "status": "error",
                    "error": f"could not parse skill output: {exc}",
                    "elapsed_seconds": f"{elapsed:.1f}",
                }
                append_row(output_path, row)
                print(
                    f"  PARSE ERROR ({elapsed:.1f}s): {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                record["elapsed_seconds"] = f"{elapsed:.1f}"
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
                        print(
                            f"  WARN: Notion write failed: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                print(
                    f"  ok ({elapsed:.1f}s): {record['name']} | {record['company']} | "
                    f"li={record['linkedin'][:40]}",
                    flush=True,
                )
                if not have_session:
                    session_marker.write_text("active", encoding="utf-8")
                    have_session = True
                urls_in_session += 1

        # Drift detection: a real search takes ~30-90s. Anything under the
        # fast-threshold while in a continued session means the model
        # shortcutted to "Not found" without doing the work — force a
        # rotation so the next URL starts fresh.
        if (
            args.fast_threshold > 0
            and have_session
            and urls_in_session > 0
            and elapsed < args.fast_threshold
        ):
            print(
                f"  drift detected ({elapsed:.1f}s < {args.fast_threshold}s) — "
                f"rotating session before next URL",
                flush=True,
            )
            have_session = False
            urls_in_session = 0
            session_marker.unlink(missing_ok=True)

        if index < len(remaining):
            if args.batch_size > 0 and index % args.batch_size == 0:
                time.sleep(args.batch_delay)
            else:
                time.sleep(args.delay)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
