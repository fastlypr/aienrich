"""Search-provider registry + waterfall for the enrichment bot.

Every provider takes a single query string and returns normalized hits in the
shape the LLM matcher already understands: {url, title, description, query}.
A provider whose API key is missing is simply skipped. The waterfall tries
providers in a configured order until one returns usable hits.

Implemented: exa, apify, brave, tavily. New providers only need a function
returning the same hit shape plus a PROVIDERS entry.
"""

from __future__ import annotations

import json
import os
from urllib.parse import quote
from urllib.request import Request, urlopen

import http_utils
from agent_search import apify_google_search
from exa_search import exa_search


def _key(cfg: dict, cfg_key: str, env: str) -> str:
    return (cfg.get(cfg_key) or os.getenv(env, "") or "").strip()


def _get_json(url: str, *, headers: dict, data: bytes | None = None, method: str = "GET") -> dict:
    req = Request(url, data=data, headers=headers, method=method)
    kwargs = {"timeout": 30}
    if http_utils.SSL_CONTEXT is not None:
        kwargs["context"] = http_utils.SSL_CONTEXT
    with urlopen(req, **kwargs) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- individual providers ---------------------------------------------------

def _exa(query: str, cfg: dict) -> list[dict]:
    key = _key(cfg, "exa_api_key", "EXA_API_KEY")
    if not key:
        return []
    return exa_search(query, key, num_results=10)


def _apify(query: str, cfg: dict) -> list[dict]:
    key = _key(cfg, "apify_token", "APIFY_TOKEN")
    if not key:
        return []
    return apify_google_search([query], key)


def _brave(query: str, cfg: dict) -> list[dict]:
    key = _key(cfg, "brave_api_key", "BRAVE_API_KEY")
    if not key:
        return []
    url = f"https://api.search.brave.com/res/v1/web/search?q={quote(query)}&count=10"
    data = _get_json(url, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    hits: list[dict] = []
    for x in (data.get("web", {}).get("results") or []):
        u = (x.get("url") or "").strip()
        if u:
            hits.append({
                "url": u,
                "title": (x.get("title") or "").strip(),
                "description": (x.get("description") or "").strip(),
                "query": query,
            })
    return hits


def _tavily(query: str, cfg: dict) -> list[dict]:
    key = _key(cfg, "tavily_api_key", "TAVILY_API_KEY")
    if not key:
        return []
    body = json.dumps({"query": query, "max_results": 10}).encode("utf-8")
    data = _get_json(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=body,
        method="POST",
    )
    hits: list[dict] = []
    for x in (data.get("results") or []):
        u = (x.get("url") or "").strip()
        if u:
            hits.append({
                "url": u,
                "title": (x.get("title") or "").strip(),
                "description": (x.get("content") or "").strip(),
                "query": query,
            })
    return hits


# --- registry ---------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "exa": {"label": "Exa.ai", "fn": _exa, "cfg_key": "exa_api_key", "env": "EXA_API_KEY"},
    "apify": {"label": "Apify (Google SERP)", "fn": _apify, "cfg_key": "apify_token", "env": "APIFY_TOKEN"},
    "brave": {"label": "Brave Search", "fn": _brave, "cfg_key": "brave_api_key", "env": "BRAVE_API_KEY"},
    "tavily": {"label": "Tavily", "fn": _tavily, "cfg_key": "tavily_api_key", "env": "TAVILY_API_KEY"},
}

DEFAULT_ORDER = ["exa", "apify", "brave", "tavily"]


def is_configured(name: str, cfg: dict) -> bool:
    p = PROVIDERS.get(name)
    return bool(p) and bool(_key(cfg, p["cfg_key"], p["env"]))


def search(name: str, query: str, cfg: dict, stats=None) -> list[dict]:
    """Run one provider. Records a search in stats if a call was made."""
    p = PROVIDERS.get(name)
    if not p or not is_configured(name, cfg):
        return []
    hits = p["fn"](query, cfg) or []
    if stats is not None:
        stats.record_search(name)
    return hits


def waterfall(order, query: str, cfg: dict, stats=None, validator=None) -> tuple[str | None, list[dict]]:
    """Try providers in order until one returns usable hits. Returns (name, hits)."""
    for name in order:
        if not is_configured(name, cfg):
            continue
        try:
            hits = search(name, query, cfg, stats)
        except Exception:
            continue
        if hits and (validator is None or validator(hits)):
            return name, hits
    return None, []
