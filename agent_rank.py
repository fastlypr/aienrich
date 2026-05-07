"""Stage 5: prefilter, score, and pick the best profile candidate.

Two passes:
  1. Heuristic — keep only valid profile URLs, score by token overlap with
     the article facts.
  2. LLM picker — when the top candidates are close, ask NIM to choose, or
     to declare 'no match'.
"""

from __future__ import annotations

import re

from agent_nvidia import NvidiaClient


_LINKEDIN_RE = re.compile(
    r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+/?$",
    re.IGNORECASE,
)
_INSTAGRAM_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/[^/?#]+/?$",
    re.IGNORECASE,
)
# Instagram URL prefixes that aren't real profile pages
_INSTAGRAM_BLOCKLIST = {
    "p",
    "reel",
    "tv",
    "explore",
    "stories",
    "accounts",
    "directory",
    "developer",
    "about",
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if len(t) > 2}


def _is_valid_profile(url: str, platform: str) -> bool:
    if platform == "linkedin":
        return bool(_LINKEDIN_RE.match(url))
    if platform == "instagram":
        if not _INSTAGRAM_RE.match(url):
            return False
        # Block /p/, /reel/, etc.
        handle = url.rstrip("/").rsplit("/", 1)[-1].lower()
        return handle not in _INSTAGRAM_BLOCKLIST
    return False


def prefilter(hits: list[dict], platform: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        url = (hit.get("url") or "").split("?")[0].split("#")[0].rstrip("/")
        if not _is_valid_profile(url, platform):
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({**hit, "url": url})
    return out


def score_hit(hit: dict, facts: dict) -> float:
    blob = f"{hit.get('title', '')} {hit.get('description', '')}"
    blob_tokens = _tokens(blob)
    score = 0.0
    weights = [
        ("name", 0.5),
        ("company", 2.0),
        ("role", 1.5),
        ("location", 1.0),
        ("industry", 0.5),
    ]
    for field, weight in weights:
        value = facts.get(field, "")
        if not value:
            continue
        value_tokens = _tokens(value)
        if not value_tokens:
            continue
        overlap = len(value_tokens & blob_tokens) / len(value_tokens)
        score += overlap * weight
    return score


def _llm_pick(
    candidates: list[dict],
    facts: dict,
    platform: str,
    client: NvidiaClient,
) -> str:
    facts_lines = [
        f"{k}: {v}" for k, v in facts.items() if v and k != "achievements"
    ]
    facts_str = "\n".join(facts_lines)
    candidates_str = "\n".join(
        f"{i + 1}. {c['url']}\n   Title: {c.get('title', '')}\n   Snippet: {c.get('description', '')}"
        for i, c in enumerate(candidates)
    )

    prompt = f"""You are matching a person from a news article to their {platform} profile.

Person facts:
{facts_str}

{platform.title()} profile candidates:
{candidates_str}

Pick the candidate that confidently matches the person above.

Return strict JSON: {{"choice": <number 1 to {len(candidates)}>, "reason": "brief"}}
If no candidate is a confident match, return: {{"choice": 0, "reason": "no match"}}
Return only JSON. No commentary."""

    try:
        result = client.chat_json(prompt, max_tokens=200)
    except Exception:
        return candidates[0]["url"]  # heuristic top, best-effort fallback

    raw_choice = result.get("choice", 0)
    try:
        choice = int(raw_choice)
    except (TypeError, ValueError):
        choice = 0

    if 1 <= choice <= len(candidates):
        return candidates[choice - 1]["url"]
    return "Not found"


def pick_best(
    hits: list[dict],
    facts: dict,
    platform: str,
    client: NvidiaClient,
    *,
    min_score: float = 1.0,
) -> str:
    """Return the chosen profile URL or the literal string 'Not found'."""
    candidates = prefilter(hits, platform)
    if not candidates:
        return "Not found"

    for c in candidates:
        c["score"] = score_hit(c, facts)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    top = candidates[:5]

    if top[0]["score"] < min_score:
        return "Not found"

    # Clear winner — skip LLM call to save tokens.
    if len(top) == 1 or top[0]["score"] >= top[1]["score"] * 2:
        return top[0]["url"]

    return _llm_pick(top, facts, platform, client)
