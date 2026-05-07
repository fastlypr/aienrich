"""Stages 3 + 4: build site-restricted Google search queries and run them
through Apify, with a primary actor + fallback chain.

Primary actor: igolaizola/google-search-scraper-ppe (id 563JCPLOqM1kMmbbP).
  - $0.15 per 1000 results, fast.
  - One query per call. We loop in Python for multiple queries.
  - Input form: {query, maxItems, countryCode, languageCode, domain}.

Fallback actor: id YNcgn7yiLc72ayYeB.
  - Used when the primary errors or returns zero hits.
  - Generic Google-search input shape.

Each call uses run-sync-get-dataset-items, which blocks until the actor
finishes and returns the dataset rows in the response body — no polling.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from http_utils import SSL_CONTEXT


APIFY_BASE = "https://api.apify.com/v2"


# ---------------------------------------------------------------------------
# Stage 3 — build queries from extracted facts
# ---------------------------------------------------------------------------

def build_queries(facts: dict, platform: str, max_queries: int = 3) -> list[str]:
    """Return up to max_queries Google search strings for the given platform.

    Queries use the platform name as a keyword rather than a site: operator,
    because Google's organic ranking surfaces real profile URLs reliably for
    natural-language queries, and SERP scraper actors handle the operator
    inconsistently. The rank stage filters returned URLs down to actual
    linkedin.com/in/ or instagram.com/<handle> profiles.
    """
    name = facts.get("name", "").strip()
    if not name or platform not in ("linkedin", "instagram"):
        return []

    company = facts.get("company", "").strip()
    role = facts.get("role", "").strip()
    location = facts.get("location", "").strip()

    queries: list[str] = []
    if company:
        queries.append(f'"{name}" "{company}" {platform}')
    if role:
        queries.append(f'"{name}" "{role}" {platform}')
    if location:
        queries.append(f'"{name}" "{location}" {platform}')
    if not queries:
        queries.append(f'"{name}" {platform}')
    return queries[:max_queries]


# ---------------------------------------------------------------------------
# Stage 4 — Apify actor invocations
# ---------------------------------------------------------------------------

def _build_input_primary(query: str, max_items: int) -> dict:
    """Input shape for igolaizola/google-search-scraper-ppe."""
    return {
        "query": query,
        "maxItems": max_items,
        "countryCode": "us",
        "languageCode": "en",
        "domain": "google.com",
    }


def _build_input_fallback(query: str, max_items: int) -> dict:
    """Input shape for actor YNcgn7yiLc72ayYeB — accepts a singular `query`
    field, similar to the primary actor."""
    return {
        "query": query,
        "maxItems": max_items,
        "countryCode": "us",
        "languageCode": "en",
    }


def _normalize_hit(item: dict, query: str) -> dict | None:
    """Coerce one search-result row into our standard {url,title,description,query} shape."""
    url = (
        item.get("url")
        or item.get("link")
        or item.get("displayedUrl")
        or ""
    )
    if not url:
        return None
    return {
        "url": str(url).strip(),
        "title": str(item.get("title") or "").strip(),
        "description": str(
            item.get("description")
            or item.get("snippet")
            or item.get("descriptionHighlighted")
            or ""
        ).strip(),
        "query": query,
    }


def _parse_dataset(items: list, query: str) -> list[dict]:
    """Walk an Apify dataset response and pull out organic-result rows.

    Handles two common output shapes:
      - flat list of result rows  (e.g. igolaizola/google-search-scraper-ppe)
      - rows containing nested 'organicResults' arrays (apify/google-search-scraper)
    """
    hits: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        organic = item.get("organicResults")
        if isinstance(organic, list) and organic:
            for entry in organic:
                if isinstance(entry, dict):
                    hit = _normalize_hit(entry, query)
                    if hit:
                        hits.append(hit)
        else:
            hit = _normalize_hit(item, query)
            if hit:
                hits.append(hit)
    return hits


def _run_actor(
    actor_id: str,
    payload: dict,
    token: str,
    *,
    timeout: int = 240,
) -> list:
    """POST to /run-sync-get-dataset-items. Blocks until the run finishes."""
    url = (
        f"{APIFY_BASE}/acts/{actor_id}"
        f"/run-sync-get-dataset-items?token={token}"
    )
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_kwargs: dict[str, Any] = {"timeout": timeout}
    if SSL_CONTEXT is not None:
        open_kwargs["context"] = SSL_CONTEXT
    try:
        with urlopen(req, **open_kwargs) as resp:
            body = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"Apify actor {actor_id} returned HTTP {exc.code}: {detail[:300]}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Apify actor {actor_id} unreachable: {exc.reason}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Apify actor {actor_id} returned non-JSON: {body[:200]}"
        )
    if not isinstance(data, list):
        raise RuntimeError(
            f"Apify actor {actor_id} returned unexpected shape (not a list)"
        )
    return data


# Actor fallback chain. Order matters — primary first. Verified working
# actor goes first; the cheaper igolaizola/google-search-scraper-ppe is kept
# as a secondary in case its issues get fixed upstream.
ACTOR_CHAIN: list[dict[str, Any]] = [
    {
        "name": "google-search-fallback (YNcgn7yiLc72ayYeB)",
        "id": "YNcgn7yiLc72ayYeB",
        "build_input": _build_input_fallback,
    },
    {
        "name": "igolaizola/google-search-scraper-ppe",
        "id": "563JCPLOqM1kMmbbP",
        "build_input": _build_input_primary,
    },
]


def search_one_query(
    query: str,
    token: str,
    *,
    max_items: int = 10,
    verbose: bool = False,
) -> list[dict]:
    """Run a single query through the actor chain. Returns hits or [].

    Tries the primary actor first; on error or empty result, falls through
    to each fallback in order.
    """
    last_error: Exception | None = None
    for actor in ACTOR_CHAIN:
        payload = actor["build_input"](query, max_items)
        try:
            items = _run_actor(actor["id"], payload, token)
        except Exception as exc:
            last_error = exc
            if verbose:
                print(f"    actor {actor['name']} failed: {exc}", flush=True)
            continue

        hits = _parse_dataset(items, query)
        if hits:
            if verbose:
                print(
                    f"    actor {actor['name']} → {len(hits)} hits "
                    f"(from {len(items)} raw items)",
                    flush=True,
                )
            return hits
        if verbose:
            print(
                f"    actor {actor['name']} returned 0 hits "
                f"(from {len(items)} raw items), trying next",
                flush=True,
            )
            if items:
                first = items[0]
                if isinstance(first, dict):
                    if "error" in first:
                        # Surface the actor's own error message — usually means
                        # input shape, quota, or proxy issue.
                        err = first.get("error")
                        print(f"      actor error: {err}", flush=True)
                    else:
                        print(
                            f"      first item keys: {sorted(first.keys())}",
                            flush=True,
                        )

    if last_error:
        raise last_error
    return []


def apify_google_search(
    queries: list[str],
    token: str,
    *,
    results_per_page: int = 10,
    verbose: bool = False,
) -> list[dict]:
    """Run each query sequentially through the actor chain. Returns the
    union of all hits, deduped by URL."""
    if not queries or not token:
        return []

    all_hits: list[dict] = []
    seen: set[str] = set()
    for query in queries:
        if verbose:
            print(f"  query: {query}", flush=True)
        try:
            hits = search_one_query(
                query, token, max_items=results_per_page, verbose=verbose
            )
        except Exception as exc:
            print(f"  WARN: search failed for {query!r}: {exc}", flush=True)
            continue
        for hit in hits:
            key = hit["url"].split("?")[0].lower()
            if key in seen:
                continue
            seen.add(key)
            all_hits.append(hit)
    return all_hits
