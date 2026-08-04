#!/usr/bin/env python3
"""
run_all.py — fast, rate-limited batch cold-email personalizer (in-place).

Input can be EITHER:
  - a local CSV path, OR
  - a PUBLIC Google Sheet URL (shared "Anyone with the link = Viewer"); it is
    downloaded as CSV.

It fetches each article and calls the model CONCURRENTLY (a thread pool) while
globally capping model requests to NVIDIA's rate limit (default 38/min, just
under the 40 cap). Then it writes results back into ONE CSV, IN PLACE:

  - For a local CSV  -> the same file you passed.
  - For a Google Sheet -> the downloaded CSV (OUT, default gsheet_leads.csv).

Data safety (by request):
  - Existing columns and values are preserved untouched; only `first_name` and
    `topic` are added/filled.
  - It NEVER adds duplicate columns — if first_name/topic already exist (any
    capitalisation) they are reused.
  - Writes are ATOMIC (temp file + os.replace), so the CSV is never left
    half-written if the run is interrupted.
  - Resumable: a row whose `topic` is already filled is skipped, so re-running
    only processes new/empty rows (and picks up new rows added to the sheet).

Usage:
  python3 run_all.py "leads.csv"
  python3 run_all.py "https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0"

Env (optional; NVIDIA_* loaded from .env automatically):
  OUT          output CSV. Google Sheet -> default gsheet_leads.csv; local CSV ->
               writes in place unless OUT is set (e.g. results/x.csv).
  RETRY        re-do leads that weren't personalized. Unset = only empty/failed
               leads are retried on a re-run (default). "failed" = also redo
               INSUFFICIENT/FETCH_FAILED. "all" = redo every lead from scratch.
  RPM          max model requests per minute (default 40)
  CONCURRENCY  worker threads / leads processed at once (default 5)
  LIMIT        process only the first N pending leads (0 = all; for testing)
  CHECKPOINT   flush the CSV every N finished leads (default 20)
  REVIEW_LOG / FAILED_LOG  log paths (default review.log / failed.log)
"""

import csv
import io
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def load_dotenv(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


class RateLimiter:
    """Spaces out model calls to <= rpm per minute, across all worker threads."""

    def __init__(self, rpm):
        self.interval = 60.0 / max(rpm, 1)
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_t:
                time.sleep(self.next_t - now)
                now = time.monotonic()
            self.next_t = now + self.interval


def extract_json(text):
    depth = 0
    start = None
    instr = False
    esc = False
    for i, c in enumerate(text):
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            instr = not instr
            continue
        if instr:
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    return ""


def clean_text(s):
    """Force plain, clean text: drop trademark/registered/copyright marks,
    straighten curly quotes/dashes, collapse odd whitespace. Models sometimes
    copy these straight out of the article (e.g. 'Sites™')."""
    if not s:
        return s
    for ch in ("™", "®", "©", "℠"):  # trademark / registered / copyright / SM
        s = s.replace(ch, "")
    trans = {
        "’": "'", "‘": "'", "‛": "'",
        "“": '"', "”": '"',
        "–": "-", "—": "-", "‑": "-", "‒": "-",
        " ": " ", " ": " ", " ": " ", " ": " ",
    }
    for a, b in trans.items():
        s = s.replace(a, b)
    return " ".join(s.split()).strip()


def pick_col(fieldnames, candidates):
    lower = {f.lower(): f for f in fieldnames}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def is_gsheet(s):
    return "docs.google.com/spreadsheets" in s


def download_gsheet(url):
    """Download a public Google Sheet as CSV text. Raises on private/parse error."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})", url)
    if not m:
        raise ValueError("could not parse a Google Sheet id from the URL")
    sheet_id = m.group(1)
    g = re.search(r"[?#&]gid=(\d+)", url)
    export = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if g:
        export += f"&gid={g.group(1)}"
    req = urllib.request.Request(export, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except Exception:
        # Some environments need an unverified SSL context as a fallback.
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            data = resp.read()
    head = data[:200].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        raise ValueError("the sheet returned HTML, not CSV — share it 'Anyone with the link = Viewer'")
    return data.decode("utf-8-sig")


def write_atomic(target_path, fieldnames, rows):
    """Write the whole table to target_path atomically (temp file + replace)."""
    parent = os.path.dirname(target_path)
    if parent:
        os.makedirs(parent, exist_ok=True)  # auto-create e.g. results/
    tmp = f"{target_path}.tmp.{os.getpid()}"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, target_path)


def process_lead(idx, full_name, article_url, linkedin, email, limiter, timeout):
    """Worker: fetch + model. Returns (idx, status, email, note, first_name, topic)."""
    if not article_url:
        return (idx, "review", email, "no_article_url", "", "")

    try:
        fetch = subprocess.run(
            [PY, os.path.join(SCRIPT_DIR, "fetch_article.py"), article_url],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (idx, "review", email, "FETCH_FAILED(timeout)", "", "")
    if fetch.returncode != 0 or len(fetch.stdout.strip()) < 1:
        return (idx, "review", email, f"FETCH_FAILED(exit={fetch.returncode})", "", "")

    limiter.wait()
    try:
        model = subprocess.run(
            [PY, os.path.join(SCRIPT_DIR, "personalize_llm.py"),
             "--full_name", full_name, "--article_url", article_url,
             "--linkedin", linkedin, "--email", email],
            input=fetch.stdout, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (idx, "failed", email, "llm_timeout", "", "")
    if model.returncode != 0:
        return (idx, "failed", email, f"llm_exit={model.returncode}: {model.stderr.strip()[:160]}", "", "")

    js = extract_json(model.stdout)
    if not js:
        return (idx, "failed", email, "no_json_in_output", "", "")
    try:
        data = json.loads(js)
    except json.JSONDecodeError as exc:
        return (idx, "failed", email, f"bad_json: {exc}", "", "")

    topic = clean_text((data.get("topic") or "").strip())
    first_name = clean_text((data.get("first_name") or "").strip())
    if topic in ("", "INSUFFICIENT", "FETCH_FAILED"):
        return (idx, "review", email, topic or "INSUFFICIENT", first_name, topic or "INSUFFICIENT")
    return (idx, "ok", email, "", first_name, topic)


def main():
    load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    os.environ.setdefault("LLM_TIMEOUT", "120")

    # Input can be passed as an argument OR set as IN in .env (e.g. a Sheet URL),
    # which keeps the run/background command short.
    in_arg = sys.argv[1] if len(sys.argv) >= 2 else os.environ.get("IN", "").strip()
    if not in_arg:
        sys.stderr.write('usage: run_all.py "<leads.csv | google-sheet-url>"   (or set IN in .env)\n')
        return 64

    rpm = float(os.environ.get("RPM", "40"))
    concurrency = int(os.environ.get("CONCURRENCY", "5"))
    limit = int(os.environ.get("LIMIT", "0"))
    checkpoint_every = int(os.environ.get("CHECKPOINT", "20"))
    review_log = os.environ.get("REVIEW_LOG", "review.log")
    failed_log = os.environ.get("FAILED_LOG", "failed.log")
    timeout = float(os.environ.get("LLM_TIMEOUT", "120")) + 30

    # --- read the source rows + decide the in-place target file ---------------
    if is_gsheet(in_arg):
        print(f"Downloading public Google Sheet...")
        try:
            text = download_gsheet(in_arg)
        except Exception as exc:
            sys.stderr.write(f"sheet download failed: {exc}\n")
            return 1
        reader = csv.DictReader(io.StringIO(text))
        source_fields = list(reader.fieldnames or [])
        rows = list(reader)
        target_path = os.environ.get("OUT", "gsheet_leads.csv")
    else:
        if not os.path.isfile(in_arg):
            sys.stderr.write(f"input file not found: {in_arg}\n")
            return 1
        with open(in_arg, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            source_fields = list(reader.fieldnames or [])
            rows = list(reader)
        target_path = os.environ.get("OUT", in_arg)  # OUT overrides; else write in place

    if not source_fields:
        sys.stderr.write("no columns found in the input.\n")
        return 1

    name_col = pick_col(source_fields, ["full_name", "Name", "name", "Full Name"])
    article_col = pick_col(source_fields, ["article_url", "article", "Article", "url", "URL"])
    linkedin_col = pick_col(source_fields, ["linkedin", "LinkedIn", "linkedin_url"])
    email_col = pick_col(source_fields, ["email", "Email", "email_address", "Email Address"])
    if not name_col or not article_col:
        sys.stderr.write(f"could not find name/article columns in: {source_fields}\n")
        return 1

    # Reuse existing first_name/topic columns (any case) — never duplicate them.
    fn_col = pick_col(source_fields, ["first_name", "First Name", "firstname"]) or "first_name"
    tp_col = pick_col(source_fields, ["topic", "Topic"]) or "topic"
    out_fields = list(source_fields)
    for c in (fn_col, tp_col):
        if c not in out_fields:
            out_fields.append(c)

    # For a Google Sheet, carry over any results already saved in the target
    # file (so re-downloading the sheet doesn't lose prior work).
    if is_gsheet(in_arg) and os.path.isfile(target_path) and email_col:
        prior = {}
        with open(target_path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                e = (r.get(email_col) or "").strip()
                if e:
                    prior[e] = (r.get(fn_col, ""), r.get(tp_col, ""))
        for row in rows:
            e = (row.get(email_col) or "").strip()
            if e in prior and prior[e][1].strip() and not (row.get(tp_col) or "").strip():
                row[fn_col], row[tp_col] = prior[e]

    # RETRY relaxes what counts as "done": default keeps any non-empty topic (so
    # only empty/failed leads are retried on re-run); "failed" also redoes
    # INSUFFICIENT/FETCH_FAILED; "all" redoes every lead.
    retry_mode = os.environ.get("RETRY", "").strip().lower()

    # Ensure every row has the two keys, and build the to-do list.
    todo = []
    already = 0
    for idx, row in enumerate(rows):
        row.setdefault(fn_col, row.get(fn_col, ""))
        row.setdefault(tp_col, row.get(tp_col, ""))
        if row.get(tp_col):
            row[tp_col] = clean_text(row[tp_col])  # scrub any pre-existing dirty topics
        tp = (row.get(tp_col) or "").strip()
        if retry_mode == "all":
            done = False
        elif retry_mode in ("1", "yes", "true", "failed"):
            done = bool(tp) and tp not in ("INSUFFICIENT", "FETCH_FAILED")
        else:
            done = bool(tp)
        if done:
            already += 1
            continue
        if limit and len(todo) >= limit:
            continue
        todo.append((idx, (row.get(name_col) or "").strip(),
                     (row.get(article_col) or "").strip(),
                     (row.get(linkedin_col) or "").strip() if linkedin_col else "",
                     (row.get(email_col) or "").strip() if email_col else ""))

    total = len(todo)
    print(f"Source: {in_arg}")
    print(f"Saving in place to: {target_path}  ({len(rows)} rows, {already} already done)")
    print(f"Model: {os.environ.get('NVIDIA_MODEL', '<default>')}  |  RPM cap: {rpm:g}  |  workers: {concurrency}")
    print(f"Processing {total} leads...")
    print("-" * 60)
    if total == 0:
        write_atomic(target_path, out_fields, rows)  # still normalise columns once
        print("Nothing to do (all rows already have a topic).")
        return 0

    limiter = RateLimiter(rpm)
    counts = {"ok": 0, "review": 0, "failed": 0}
    started = time.monotonic()
    n_done = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(process_lead, *t, limiter, timeout) for t in todo]
        for fut in as_completed(futures):
            idx, status, email, note, first_name, topic = fut.result()
            with lock:
                n_done += 1
                counts[status] += 1
                rows[idx][fn_col] = first_name
                rows[idx][tp_col] = topic
                if status == "ok":
                    print(f"[{n_done}/{total}] OK    {email}  ->  {topic}")
                elif status == "review":
                    with open(review_log, "a", encoding="utf-8") as lf:
                        lf.write(f"{email} | {note}\n")
                    print(f"[{n_done}/{total}] SKIP  {email}  ({note})")
                else:
                    with open(failed_log, "a", encoding="utf-8") as lf:
                        lf.write(f"{email} | {note}\n")
                    print(f"[{n_done}/{total}] FAIL  {email}  ({note})")
                if n_done % checkpoint_every == 0:
                    write_atomic(target_path, out_fields, rows)

    write_atomic(target_path, out_fields, rows)
    elapsed = time.monotonic() - started
    rate = (n_done / elapsed * 60) if elapsed else 0
    print("-" * 60)
    print(f"Done in {elapsed/60:.1f} min  ({rate:.0f} leads/min).")
    print(f"  ok={counts['ok']}  review={counts['review']}  failed={counts['failed']}")
    print(f"  saved in place: {target_path}")
    if counts["failed"]:
        print(f"  re-run the same command to retry the {counts['failed']} failed lead(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
