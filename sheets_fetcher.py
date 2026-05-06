"""Read URLs from a public Google Sheet via the CSV export endpoint.

The sheet must be shared as 'Anyone with the link can view' — no OAuth or
service account is used. This module is stdlib-only.
"""

from __future__ import annotations

import csv
import io
import re
from urllib.request import urlopen

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_GID_RE = re.compile(r"[#&?]gid=([0-9]+)")


def parse_sheet_url(url: str) -> tuple[str, str]:
    """Return (sheet_id, gid). gid defaults to '0' if missing."""
    match = _SHEET_ID_RE.search(url)
    if match:
        sheet_id = match.group(1)
    elif re.fullmatch(r"[a-zA-Z0-9-_]{20,}", url.strip()):
        sheet_id = url.strip()
    else:
        raise ValueError(f"Could not extract a sheet id from: {url}")

    gid_match = _GID_RE.search(url)
    gid = gid_match.group(1) if gid_match else "0"
    return sheet_id, gid


def fetch_urls(sheet_url: str, column: str = "URL") -> list[str]:
    sheet_id, gid = parse_sheet_url(sheet_url)
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )
    with urlopen(csv_url, timeout=30) as resp:
        body = resp.read().decode("utf-8")

    reader = csv.DictReader(io.StringIO(body))
    if reader.fieldnames is None:
        return []

    target = None
    for name in reader.fieldnames:
        if name and name.strip().lower() == column.strip().lower():
            target = name
            break
    if not target:
        raise ValueError(
            f"Column '{column}' not found in sheet. Available headers: {reader.fieldnames}"
        )

    urls: list[str] = []
    seen: set[str] = set()
    for row in reader:
        value = (row.get(target) or "").strip()
        if not value or not value.lower().startswith(("http://", "https://")):
            continue
        if value in seen:
            continue
        seen.add(value)
        urls.append(value)
    return urls
