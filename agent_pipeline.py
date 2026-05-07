"""Agent pipeline: URL -> facts -> search -> rank -> verify -> final record.

Each stage is independent. Failures in one stage degrade gracefully — the
pipeline still produces a record with 'Not found' for whatever couldn't be
resolved, rather than aborting.

The verify stage uses the search-result snippet (already in memory from the
Apify search), not a fresh fetch of the profile page. LinkedIn and
Instagram block unauthenticated meta requests, so re-fetching wouldn't help
anyway — the snippet has all the information Google indexed.
"""

from __future__ import annotations

import re

from agent_extract import extract_facts
from agent_fetch import fetch_article_text
from agent_nvidia import NvidiaClient
from agent_rank import pick_best
from agent_search import apify_google_search, build_queries
from agent_verify import snippet_matches


def _category(facts: dict, linkedin_hit: dict | None, client: NvidiaClient) -> str:
    role = facts.get("role", "")
    industry = facts.get("industry", "")
    headline = ""
    if linkedin_hit:
        # The LinkedIn search snippet typically reads
        # "Name - Role - Company | LinkedIn" which is exactly the headline.
        headline = (
            linkedin_hit.get("title", "")
            or linkedin_hit.get("description", "")
            or ""
        )

    prompt = f"""Generate a 1 to 2 word professional category for the person below.

Rules:
- Lowercase only
- Singular only (entrepreneur, not entrepreneurs)
- 1 or 2 words exactly — no more, no less
- No punctuation, no quotes
- No name, no location, no company

Information:
Role: {role}
Industry: {industry}
LinkedIn headline: {headline}

If unclear, return: public figure
Return only the category phrase, nothing else."""

    try:
        text = client.chat(prompt, max_tokens=20, temperature=0.1).strip().lower()
    except Exception:
        return "public figure"

    text = re.sub(r"[^a-z ]+", "", text).strip()
    words = text.split()
    if not words or len(words) > 2:
        return "public figure"
    return text


def _find_hit(hits: list[dict], url: str) -> dict | None:
    """Locate the search hit that matches a chosen URL."""
    if not url or url == "Not found":
        return None
    target = url.lower().rstrip("/")
    for hit in hits:
        candidate = (hit.get("url") or "").lower().rstrip("/")
        if candidate == target:
            return hit
    return None


def enrich_url(
    url: str,
    client: NvidiaClient,
    apify_token: str | None,
    *,
    verbose: bool = True,
) -> dict:
    def log(msg: str) -> None:
        if verbose:
            print(f"  {msg}", flush=True)

    record = {
        "url": url,
        "name": "Not found",
        "company": "Not found",
        "linkedin": "Not found",
        "instagram": "Not found",
        "category": "public figure",
        "status": "ok",
        "error": "",
    }

    try:
        # Stage 1 — fetch article text
        log("fetching article…")
        text = fetch_article_text(url)
        if len(text) < 200:
            record["status"] = "error"
            record["error"] = "Article body too short or unreachable"
            return record

        # Stage 2 — extract facts
        log("extracting facts…")
        facts = extract_facts(text, client)
        if not facts.get("name"):
            log("no main person identified")
            return record
        record["name"] = facts["name"]
        record["company"] = facts.get("company") or "Not found"
        log(f"person: {facts['name']} | company: {facts.get('company') or '—'}")

        # Stages 3 + 4 — build queries, run Apify search
        if apify_token:
            log("searching LinkedIn…")
            li_queries = build_queries(facts, "linkedin")
            li_hits = (
                apify_google_search(li_queries, apify_token) if li_queries else []
            )
            log(f"  {len(li_hits)} hits across {len(li_queries)} queries")

            log("searching Instagram…")
            ig_queries = build_queries(facts, "instagram")
            ig_hits = (
                apify_google_search(ig_queries, apify_token) if ig_queries else []
            )
            log(f"  {len(ig_hits)} hits across {len(ig_queries)} queries")
        else:
            log("APIFY_TOKEN missing — skipping search (LinkedIn/Instagram will be Not found)")
            li_hits = []
            ig_hits = []

        # Stage 5 — rank candidates
        if li_hits:
            record["linkedin"] = pick_best(li_hits, facts, "linkedin", client)
        if ig_hits:
            record["instagram"] = pick_best(ig_hits, facts, "instagram", client)

        # Stage 6 — verify against the search snippets we already have
        chosen_li = _find_hit(li_hits, record["linkedin"])
        if chosen_li and not snippet_matches(chosen_li, facts):
            log(f"  LinkedIn snippet doesn't match — demoting to Not found")
            record["linkedin"] = "Not found"
            chosen_li = None

        chosen_ig = _find_hit(ig_hits, record["instagram"])
        if chosen_ig and not snippet_matches(chosen_ig, facts):
            log(f"  Instagram snippet doesn't match — demoting to Not found")
            record["instagram"] = "Not found"

        # Stage 7 — category
        log("generating category…")
        record["category"] = _category(facts, chosen_li, client)

    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"

    return record
