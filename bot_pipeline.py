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
from exa_search import build_query_linkedin, build_query_website


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
            log("article too short/unreachable")
            return rec
        log(f"fetched {len(text):,} chars")

        log("extracting facts + category (LLM)…")
        facts = extract_facts_with_category(text, client)
        rec["category"] = facts["category"]
        if not facts.get("name"):
            log("no main person identified — skipping")
            return rec
        rec["name"] = facts["name"]
        rec["company"] = facts.get("company") or "Not found"
        log(f"extracted: {rec['name']} · {rec['company']} · {facts['category']}")

        # Two separate searches — one biased for the LinkedIn profile, one for
        # the website — then merge the candidates so each has its best shot.
        li_q = build_query_linkedin(facts)
        web_q = build_query_website(facts)
        providers: list[str] = []
        hits: list[dict] = []
        seen: set[str] = set()
        for label, q in (("linkedin", li_q), ("website", web_q)):
            if not q:
                continue
            log(f"searching ({label}): {q}")
            prov, h = do_search(q)
            if prov:
                providers.append(prov)
            log(f"  {len(h)} result(s)" + (f" via {prov}" if prov else ""))
            for hit in h:
                key = (hit.get("url") or "").split("?")[0].rstrip("/").lower()
                if key and key not in seen:
                    seen.add(key)
                    hits.append(hit)
        rec["provider"] = providers[0] if providers else ""
        log(f"{len(hits)} combined candidate(s)")
        if not hits:
            return rec

        log("matching profiles (LLM)…")
        matched = match_profiles(facts, hits, client, article_url=url)
        rec["linkedin"] = matched["linkedin"]
        rec["website"] = matched["website"]
        log(f"matched → LinkedIn={rec['linkedin']} · Website={rec['website']}")
    except Exception as exc:  # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        log(f"ERROR: {rec['error']}")
    return rec
