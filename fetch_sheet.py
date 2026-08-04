#!/usr/bin/env python3
"""Pull a column of URLs from a public Google Sheet into a text file.

Uses the gviz CSV endpoint (more reliable than /export) and the repo's shared
SSL context, so it works on macOS python.org builds where naked urlopen() over
HTTPS fails. The sheet must be shared "Anyone with the link can view".

Usage:
    python3 fetch_sheet.py <sheet_url> [column] [output_file]

Defaults: column="Article URL", output_file="urls.txt"
"""

from __future__ import annotations

import csv
import io
import sys
from urllib.request import urlopen

import http_utils
import sheets_fetcher
import url_utils


def _csv_url(sheet_url: str) -> str:
    sid, gid = sheets_fetcher.parse_sheet_url(sheet_url)
    return f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={gid}"


def get_headers(sheet_url: str) -> list[str]:
    """Return the column headers of the sheet's first row."""
    kwargs = {"timeout": 30}
    if http_utils.SSL_CONTEXT is not None:
        kwargs["context"] = http_utils.SSL_CONTEXT
    with urlopen(_csv_url(sheet_url), **kwargs) as resp:
        body = resp.read().decode("utf-8", "ignore")
    reader = csv.DictReader(io.StringIO(body))
    return [h for h in (reader.fieldnames or []) if h]


def guess_url_column(headers: list[str]) -> str | None:
    """Best-guess the column holding article URLs. None if ambiguous."""
    candidates = [h for h in headers if any(k in h.lower() for k in ("url", "link", "article"))]
    if len(candidates) == 1:
        return candidates[0]
    # exact common names win outright
    for exact in ("Article URL", "URL", "Link"):
        for h in headers:
            if h.strip().lower() == exact.lower():
                return h
    return None


def guess_name_column(headers: list[str]) -> str | None:
    """Best-guess the column holding the person's name."""
    for exact in ("full_name", "Full Name", "Name", "name"):
        for h in headers:
            if h.strip().lower() == exact.lower():
                return h
    cands = [h for h in headers if "name" in h.lower()
             and not any(k in h.lower() for k in ("company", "first", "last", "user"))]
    return cands[0] if len(cands) == 1 else None


def fetch_urls(sheet_url: str, column: str = "Article URL") -> list[str]:
    sid, gid = sheets_fetcher.parse_sheet_url(sheet_url)
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={gid}"
    )
    kwargs = {"timeout": 30}
    if http_utils.SSL_CONTEXT is not None:
        kwargs["context"] = http_utils.SSL_CONTEXT
    with urlopen(csv_url, **kwargs) as resp:
        body = resp.read().decode("utf-8", "ignore")

    reader = csv.DictReader(io.StringIO(body))
    fieldnames = reader.fieldnames or []
    col = next(
        (c for c in fieldnames if c and c.strip().lower() == column.strip().lower()),
        None,
    )
    if not col:
        raise SystemExit(
            f"Column {column!r} not found. Available headers: {fieldnames}"
        )

    urls: list[str] = []
    seen: set[str] = set()
    for row in reader:
        raw = (row.get(col) or "").strip()
        if not raw or " " in raw or "." not in raw:
            continue
        normalized = url_utils.normalize(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sheet_url = sys.argv[1]
    column = sys.argv[2] if len(sys.argv) > 2 else "Article URL"
    out = sys.argv[3] if len(sys.argv) > 3 else "urls.txt"

    urls = fetch_urls(sheet_url, column)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(urls))
    print(f"wrote {len(urls)} URL(s) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
