"""Unified enrichment: fetch -> extract(+category) -> search -> match.

Same steps as exa_pipeline, but the search step is injected as a callable so
the caller can pick a single provider or run a waterfall. Reuses the existing
extract and match LLM helpers unchanged.

    do_search(query) -> (provider_name | None, hits)
"""

from __future__ import annotations

from typing import Callable

from agent_fetch import fetch_article_text
from agent_nvidia import NvidiaClient
from exa_pipeline import extract_facts_with_category, match_profiles
from exa_search import build_query


def enrich(
    url: str,
    client: NvidiaClient,
    do_search: Callable[[str], tuple[str | None, list[dict]]],
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> dict:
    rec = {
        "url": url,
        "name": "Not found",
        "company": "Not found",
        "linkedin": "Not found",
        "website": "Not found",
        "category": "public figure",
        "status": "ok",
        "error": "",
        "provider": "",
    }
    try:
        log("fetching article…")
        text = fetch_article_text(url)
        if len(text) < 200:
            rec["status"] = "error"
            rec["error"] = "Article body too short or unreachable"
            return rec

        log("extracting facts + category…")
        facts = extract_facts_with_category(text, client)
        rec["category"] = facts["category"]
        if not facts.get("name"):
            log("no main person identified")
            return rec
        rec["name"] = facts["name"]
        rec["company"] = facts.get("company") or "Not found"

        query = build_query(facts)
        log(f"search: {query}")
        provider, hits = do_search(query)
        rec["provider"] = provider or ""
        log(f"  {len(hits)} result(s)" + (f" via {provider}" if provider else " (no provider)"))
        if not hits:
            return rec

        log("matching profiles…")
        matched = match_profiles(facts, hits, client)
        rec["linkedin"] = matched["linkedin"]
        rec["website"] = matched["website"]
    except Exception as exc:  # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec
