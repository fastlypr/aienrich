"""Exa.ai search for the Exa enrichment workflow.

Exa is used purely as the search engine — it takes one query per person and
returns candidate result URLs (LinkedIn, personal/company website, etc.). All
extraction and matching is done locally by the NVIDIA LLM elsewhere.

Two helpers:
  build_query(facts) -> str          build one query string from extracted facts
  exa_search(query, key) -> [hit]    run the query, return normalized hits
"""

from __future__ import annotations

from typing import Any


def build_query(facts: dict) -> str:
    """Build one search query per person.

    Format: `{name} {company} LinkedIn & website` — all terms unquoted. The
    company is NOT quoted: exact-phrase quoting filters out the right profile
    when the person's LinkedIn lists a rebranded/abbreviated company name
    (e.g. article says "BrainStorm Academic Solutions" but LinkedIn says
    "BrainStorm Tutoring"). Falls back to role if there is no company, and to
    the bare name if neither is present.
    """
    name = (facts.get("name") or "").strip()
    if not name:
        return ""

    company = (facts.get("company") or "").strip()
    role = (facts.get("role") or "").strip()

    if company:
        anchor = f" {company}"
    elif role:
        anchor = f" {role}"
    else:
        anchor = ""

    return f"{name}{anchor} LinkedIn & website".strip()


def build_query_linkedin(facts: dict) -> str:
    """`{name} {company} linkedin` (falls back to role, then bare name)."""
    name = (facts.get("name") or "").strip()
    if not name:
        return ""
    company = (facts.get("company") or "").strip()
    role = (facts.get("role") or "").strip()
    anchor = f" {company}" if company else (f" {role}" if role else "")
    return f"{name}{anchor} linkedin".strip()


def build_query_website(facts: dict) -> str:
    """`{name} {company} website` (falls back to bare name)."""
    name = (facts.get("name") or "").strip()
    if not name:
        return ""
    company = (facts.get("company") or "").strip()
    anchor = f" {company}" if company else ""
    return f"{name}{anchor} website".strip()


def _highlights_to_text(result: Any) -> str:
    """Pull a snippet string out of an Exa result's highlights/text."""
    highlights = getattr(result, "highlights", None)
    if highlights is None and isinstance(result, dict):
        highlights = result.get("highlights")
    if isinstance(highlights, (list, tuple)):
        joined = " ".join(str(h).strip() for h in highlights if h)
        if joined:
            return joined

    text = getattr(result, "text", None)
    if text is None and isinstance(result, dict):
        text = result.get("text")
    return str(text or "").strip()


def _normalize(result: Any, query: str) -> dict | None:
    """Coerce one Exa result into {url, title, description, query}."""
    url = getattr(result, "url", None)
    title = getattr(result, "title", None)
    if url is None and isinstance(result, dict):
        url = result.get("url")
        title = result.get("title")
    url = str(url or "").strip()
    if not url:
        return None
    return {
        "url": url,
        "title": str(title or "").strip(),
        "description": _highlights_to_text(result),
        "query": query,
    }


def exa_search(
    query: str,
    api_key: str,
    *,
    num_results: int = 10,
) -> list[dict]:
    """Run one Exa query and return normalized hits. Empty list on no query."""
    if not query or not api_key:
        return []

    from exa_py import Exa  # imported lazily so the dep is only needed here

    exa = Exa(api_key)
    response = exa.search(
        query,
        num_results=num_results,
        type="auto",
        contents={"highlights": True},
    )

    results = getattr(response, "results", None)
    if results is None and isinstance(response, dict):
        results = response.get("results")
    results = results or []

    hits: list[dict] = []
    seen: set[str] = set()
    for result in results:
        hit = _normalize(result, query)
        if not hit:
            continue
        key = hit["url"].split("?")[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)
    return hits
