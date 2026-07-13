"""Exa enrichment pipeline: URL -> facts -> Exa search -> matched profiles.

Two LLM calls per URL (both NVIDIA NIM):
  1. extract_facts_with_category — read the article once, return the person's
     facts AND the 1-2 word category in a single JSON response. Also surfaces
     any LinkedIn/website URLs written in the article body (strong hints).
  2. match_profiles — given the facts + the Exa search results, pick the
     matching LinkedIn and website, or abstain ("Not found") when nothing
     confidently matches. This is how the "same name, wrong profile" case is
     handled — the model is told to return 0 rather than guess.

Article fetch, the NVIDIA client, and the records all reuse the existing
local helpers. Nothing in the Apify workflow is touched.
"""

from __future__ import annotations

import re
import time

from agent_fetch import fetch_article_text
from agent_nvidia import NvidiaClient
from exa_search import build_query, exa_search


def _chat_json_retry(
    client: NvidiaClient,
    prompt: str,
    *,
    max_tokens: int,
    retries: int = 5,
    base_delay: float = 5.0,
) -> dict:
    """client.chat_json with exponential backoff on rate-limit (429) errors.

    The NVIDIA free tier rate-limits bursts of requests. Rather than failing
    the URL, wait and retry with growing delays (5s, 10s, 20s, …). Non-429
    errors are raised immediately.
    """
    attempt = 0
    while True:
        try:
            return client.chat_json(prompt, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - inspect message for 429
            msg = str(exc)
            is_rate_limit = "429" in msg or "rate" in msg.lower()
            if not is_rate_limit or attempt >= retries:
                raise
            wait = base_delay * (2 ** attempt)
            print(f"    rate-limited (429); waiting {wait:.0f}s then retrying…", flush=True)
            time.sleep(wait)
            attempt += 1


# ---------------------------------------------------------------------------
# LLM call #1 — extract facts + category in one pass
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """You are reading a news article and extracting facts about \
the main person featured, plus a professional category.

Identify the main person — usually the interviewee, the founder/executive being \
profiled, or the named subject in the headline. Ignore quoted experts, \
journalists, photographers, and supporting names. If multiple people are \
co-featured equally, pick the one named first in the headline or byline.

Return a JSON object with exactly these fields:
{
  "name": "Full name (exact spelling from the article); empty string if no clear main person",
  "company": "Company or organization; empty string if none",
  "role": "Job title or role; empty string if none",
  "location": "City/region if mentioned; empty string if none",
  "industry": "Industry or field; empty string if unclear",
  "category": "A 1 or 2 word professional category, lowercase, singular (e.g. entrepreneur, chef, investor); 'public figure' if unclear",
  "article_linkedin": "A linkedin.com profile URL if one literally appears in the article text; empty string otherwise",
  "article_website": "The person's personal or company website URL if one literally appears in the article text; empty string otherwise"
}

Rules:
- Use facts present in the article only. Do not infer or guess.
- category: lowercase, 1 or 2 words exactly, no punctuation, no name/company/location.
- article_linkedin / article_website: only fill these if the URL is actually \
written in the article. Do not invent URLs.
- If the article is about a company rather than a person, identify the founder \
or CEO if clearly the focus, otherwise return empty strings.

Return only valid JSON. No markdown fences. No commentary."""


def _clean_category(value: str) -> str:
    text = re.sub(r"[^a-z ]+", "", str(value or "").strip().lower()).strip()
    words = text.split()
    if not words or len(words) > 2:
        return "public figure"
    return text


def extract_facts_with_category(article_text: str, client: NvidiaClient) -> dict:
    prompt = f"{_EXTRACT_PROMPT}\n\nArticle text:\n{article_text}"
    data = _chat_json_retry(client, prompt, max_tokens=1024)

    return {
        "name": str(data.get("name") or "").strip(),
        "company": str(data.get("company") or "").strip(),
        "role": str(data.get("role") or "").strip(),
        "location": str(data.get("location") or "").strip(),
        "industry": str(data.get("industry") or "").strip(),
        "category": _clean_category(data.get("category")),
        "article_linkedin": str(data.get("article_linkedin") or "").strip(),
        "article_website": str(data.get("article_website") or "").strip(),
    }


# ---------------------------------------------------------------------------
# LLM call #2 — match the right LinkedIn + website from the Exa results
# ---------------------------------------------------------------------------

_SOCIAL_HOSTS = (
    "linkedin.com",
    "instagram.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "pinterest.com",
    "threads.net",
)


def _match_prompt(facts: dict, results: list[dict]) -> str:
    facts_lines = []
    for key in ("name", "company", "role", "location", "industry"):
        value = facts.get(key)
        if value:
            facts_lines.append(f"{key}: {value}")
    hint_lines = []
    if facts.get("article_linkedin"):
        hint_lines.append(f"LinkedIn URL found in the article: {facts['article_linkedin']}")
    if facts.get("article_website"):
        hint_lines.append(f"Website URL found in the article: {facts['article_website']}")
    hints = ("\n" + "\n".join(hint_lines)) if hint_lines else ""

    candidates = "\n".join(
        f"{i + 1}. {r['url']}\n   Title: {r.get('title', '')}\n   Snippet: {r.get('description', '')}"
        for i, r in enumerate(results)
    )

    return f"""You are matching a person from a news article to their LinkedIn \
profile and their personal/company website, using search results.

Person facts:
{chr(10).join(facts_lines)}{hints}

Search results:
{candidates}

Choose:
- The result that is THIS person's LinkedIn profile (a linkedin.com/in/... page).
- The result that is THIS person's personal or company website (NOT a social \
network, news site, directory, or aggregator).

Be strict. Only choose a candidate when the name AND at least one of \
company/role/location confidently agree. People often share a name — if you \
are not confident a result is the SAME person, do not choose it.

Return strict JSON: {{"linkedin": <result number or 0>, "website": <result number or 0>}}
Use 0 when no result confidently matches. Return only JSON. No commentary."""


def _is_linkedin_profile(url: str) -> bool:
    return bool(re.match(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+", url, re.I))


def _is_website(url: str) -> bool:
    low = url.lower()
    return not any(host in low for host in _SOCIAL_HOSTS)


def match_profiles(
    facts: dict, results: list[dict], client: NvidiaClient
) -> dict:
    """Return {"linkedin": url|"Not found", "website": url|"Not found"}."""
    out = {"linkedin": "Not found", "website": "Not found"}
    if not results:
        return out

    try:
        data = _chat_json_retry(client, _match_prompt(facts, results), max_tokens=200)
    except Exception:
        return out

    def _pick(field: str, validator) -> str:
        try:
            choice = int(data.get(field, 0))
        except (TypeError, ValueError):
            choice = 0
        if 1 <= choice <= len(results):
            url = results[choice - 1]["url"]
            if validator(url):
                return url
        return "Not found"

    out["linkedin"] = _pick("linkedin", _is_linkedin_profile)
    out["website"] = _pick("website", _is_website)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def enrich_url_exa(
    url: str,
    client: NvidiaClient,
    exa_key: str | None,
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
        "website": "Not found",
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

        # Stage 2 — extract facts + category (LLM call #1)
        log("extracting facts + category…")
        facts = extract_facts_with_category(text, client)
        record["category"] = facts["category"]
        if not facts.get("name"):
            log("no main person identified")
            return record
        record["name"] = facts["name"]
        record["company"] = facts.get("company") or "Not found"
        log(f"person: {facts['name']} | company: {facts.get('company') or '—'}")

        # Stage 3 + 4 — build one query, search Exa
        if not exa_key:
            log("EXA_API_KEY missing — skipping search (LinkedIn/website will be Not found)")
            return record

        query = build_query(facts)
        log(f"exa query: {query}")
        results = exa_search(query, exa_key, num_results=10)
        log(f"  {len(results)} result(s)")
        if not results:
            return record

        # Stage 5 — match the right LinkedIn + website (LLM call #2)
        log("matching profiles…")
        matched = match_profiles(facts, results, client)
        record["linkedin"] = matched["linkedin"]
        record["website"] = matched["website"]
        log(f"  linkedin: {record['linkedin']} | website: {record['website']}")

    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"

    return record
