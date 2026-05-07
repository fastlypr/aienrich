"""Stages 3 + 4: build search queries, run them via Apify Google Search.

Uses Apify's REST API directly (no SDK dependency) — POST to the
run-sync-get-dataset-items endpoint, get organic results back.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


GOOGLE_SEARCH_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"


def build_queries(facts: dict, platform: str, max_queries: int = 3) -> list[str]:
    """Return up to max_queries Google search strings for the given platform.

    platform must be 'linkedin' or 'instagram'. Queries are skipped if their
    required facts are missing.
    """
    name = facts.get("name", "").strip()
    if not name:
        return []

    company = facts.get("company", "").strip()
    role = facts.get("role", "").strip()
    location = facts.get("location", "").strip()

    site_op = {
        "linkedin": "site:linkedin.com/in",
        "instagram": "site:instagram.com",
    }.get(platform)
    if not site_op:
        return []

    queries: list[str] = []
    if company:
        queries.append(f'"{name}" "{company}" {site_op}')
    if role:
        queries.append(f'"{name}" "{role}" {site_op}')
    if location:
        queries.append(f'"{name}" "{location}" {site_op}')
    if not queries:
        queries.append(f'"{name}" {site_op}')
    return queries[:max_queries]


def apify_google_search(
    queries: list[str],
    token: str,
    *,
    results_per_page: int = 10,
    timeout: int = 90,
) -> list[dict]:
    """Run the Apify Google Search Scraper for the given queries.

    Returns a flat list of {url, title, description, query} dicts pooled
    across all queries.
    """
    if not queries or not token:
        return []

    url = (
        f"{APIFY_BASE}/acts/{GOOGLE_SEARCH_ACTOR}"
        f"/run-sync-get-dataset-items?token={token}"
    )
    payload = {
        "queries": "\n".join(queries),
        "resultsPerPage": results_per_page,
        "maxPagesPerQuery": 1,
        "saveHtml": False,
        "saveHtmlToKeyValueStore": False,
        "mobileResults": False,
    }

    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Apify {exc.code}: {detail}") from exc

    items = json.loads(body)
    hits: list[dict] = []
    for item in items:
        query = ""
        sq = item.get("searchQuery") or {}
        if isinstance(sq, dict):
            query = sq.get("term") or ""
        for org in item.get("organicResults") or []:
            hits.append(
                {
                    "url": (org.get("url") or "").strip(),
                    "title": (org.get("title") or "").strip(),
                    "description": (org.get("description") or "").strip(),
                    "query": query,
                }
            )
    return hits
