#!/usr/bin/env python3
"""Enrich URLs with opencode.ai + a pre-installed skill.

This runner shells out to `opencode run "<prompt>"` per URL. The user's
profile-finder skill handles fetching, searching, verifying, and writing
to Notion — our pipeline just hands it URLs one at a time and tracks
progress in results_opencode.csv for dedup + resume.

Session reuse:
  - First URL: fresh opencode session.
  - URLs 2..N: --continue (-c) to reuse the same opencode session, so the
    skill's system prompt isn't re-billed every URL.
  - --rotate-after N starts a fresh session every N URLs, capping
    conversation history bloat.
  - --fresh-session forces a new session for this run.

The skill is responsible for the Notion write, so this runner doesn't use
notion_writer.py. The CSV is just a "URL was handed to the skill, exit
code 0 = succeeded" marker for dedup.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path

import agent_config
import config
import sheets_fetcher
import url_utils


CSV_FIELDS = [
    "url",
    "status",
    "error",
    "elapsed_seconds",
]


SESSION_MARKER = ".opencode_session_active"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich URLs via opencode.ai + the profile-finder skill."
    )
    parser.add_argument("--input", default="urls.txt")
    parser.add_argument("--output", default="results_opencode.csv")
    parser.add_argument(
        "--model",
        default="",
        help="opencode model id (e.g. 'opencode/minimax-m2.5'). "
        "Run `opencode models` to find available ids. "
        "Leave empty to use opencode's configured default.",
    )
    parser.add_argument(
        "--agent",
        default="",
        help="opencode agent name to use, if your skill is wrapped in one.",
    )
    parser.add_argument(
        "--prompt-template",
        default=(
            "Process this article URL: extract the main person, find their "
            "LinkedIn and Instagram profiles, then save the result to the "
            "Notion database. URL: {url}"
        ),
        help="Template sent to opencode. {url} is replaced.",
    )
    parser.add_argument("--reconfigure", action="store_true")
    parser.add_argument("--no-sheet", action="store_true")
    parser.add_argument("--fresh-session", action="store_true")
    parser.add_argument(
        "--rotate-after",
        type=int,
        default=50,
        help="Start a fresh opencode session every N URLs. 0 to disable.",
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


def run_opencode(
    url: str,
    *,
    template: str,
    model: str,
    agent: str,
    continue_session: bool,
    timeout: int,
) -> tuple[bool, str, float]:
    """Invoke `opencode run` with the URL prompt. Returns (success, error_msg, seconds)."""
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
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s", time.time() - started
    except FileNotFoundError:
        return (
            False,
            "opencode binary not found on PATH. Install it or fix PATH.",
            time.time() - started,
        )

    elapsed = time.time() - started
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        return False, f"exit {result.returncode}: {msg[:300]}", elapsed
    return True, "", elapsed


def main() -> int:
    args = parse_args()

    agent_config.load_dotenv()

    cfg = config.load()
    cfg = config.prompt_for_missing(cfg, reconfigure=args.reconfigure)

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
    if args.model:
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
                f"Rotating to a fresh opencode session after {urls_in_session} URLs."
            )
            have_session = False
            urls_in_session = 0
            session_marker.unlink(missing_ok=True)

        success, error_msg, elapsed = run_opencode(
            url,
            template=args.prompt_template,
            model=args.model,
            agent=args.agent,
            continue_session=have_session,
            timeout=args.timeout,
        )

        row = {
            "url": url,
            "status": "ok" if success else "error",
            "error": error_msg,
            "elapsed_seconds": f"{elapsed:.1f}",
        }
        append_row(output_path, row)

        if success:
            print(f"  ok ({elapsed:.1f}s)", flush=True)
            if not have_session:
                # First successful run — session now exists
                session_marker.write_text("active", encoding="utf-8")
                have_session = True
            urls_in_session += 1
        else:
            print(f"  ERROR ({elapsed:.1f}s): {error_msg}", file=sys.stderr, flush=True)

        if index < len(remaining):
            if args.batch_size > 0 and index % args.batch_size == 0:
                time.sleep(args.batch_delay)
            else:
                time.sleep(args.delay)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
