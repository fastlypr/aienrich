"""Stage 6: verify a chosen profile URL.

LinkedIn and Instagram block unauthenticated meta requests, so og:title and
og:description from a fresh GET are useless on real profile pages. Instead
we verify against the Apify search-result snippet (already in memory from
the search stage). The snippet is essentially the same information Google
indexed when the page was first crawled — name, role, company, location —
which is exactly what we need to confirm the match.

Two helpers:
  - snippet_matches(hit, facts): the new, preferred check
  - is_match(meta, facts, url): legacy helper kept for the rare case where
    we already have og:meta from a non-platform site
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from agent_fetch import fetch_html


def snippet_matches(hit: dict, facts: dict) -> bool:
    """True if the search-result hit plausibly matches the person's facts.

    Combines title + description + URL into one blob and checks for at
    least half the name tokens. The URL slug carries the strongest signal
    (e.g., /in/corbin-cornwell), title carries headline info, description
    carries the snippet Google extracted when crawling.
    """
    name = facts.get("name", "").strip()
    if not name:
        return True

    name_parts = [
        p.lower() for p in re.findall(r"[a-zA-Z']+", name) if len(p) > 2
    ]
    if not name_parts:
        return True

    blob = " ".join(
        [
            hit.get("title", "") or "",
            hit.get("description", "") or "",
            hit.get("url", "") or "",
        ]
    ).lower()
    if not blob:
        return False

    present = sum(1 for p in name_parts if p in blob)
    return present / len(name_parts) >= 0.5


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.metas: dict[str, str] = {}
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "meta":
            attrs_d = dict(attrs)
            name = (attrs_d.get("property") or attrs_d.get("name") or "").lower()
            content = attrs_d.get("content") or ""
            if name and content:
                self.metas[name] = content
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def fetch_meta(url: str, timeout: int = 20) -> dict:
    try:
        html = fetch_html(url, timeout=timeout)
    except Exception:
        return {}
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    return {
        "title": parser.title.strip(),
        "og_title": parser.metas.get("og:title", ""),
        "og_description": parser.metas.get("og:description", ""),
    }


def is_match(meta: dict, facts: dict, url: str = "") -> bool:
    """True if the URL or meta data plausibly matches the person's facts.

    LinkedIn and Instagram aggressively block unauthenticated meta requests,
    so og:title / og:description are often generic ("LinkedIn", "Instagram")
    even on real profile pages. To avoid false demotion, we accept the URL
    if EITHER signal matches:

      1. The URL slug contains at least half the name tokens (very strong —
         platform handles like /in/corbin-cornwell or /corbincornwell are
         picked from the actual person's name)
      2. The og:title / og:description blob covers at least half the name

    A profile is only demoted when BOTH signals fail.
    """
    name = facts.get("name", "").strip()
    if not name:
        return True

    name_parts = [
        p.lower() for p in re.findall(r"[a-zA-Z']+", name) if len(p) > 2
    ]
    if not name_parts:
        return True

    # Signal 1 — URL slug contains the name
    url_lower = url.lower()
    url_hits = sum(1 for p in name_parts if p in url_lower)
    if url_hits / len(name_parts) >= 0.5:
        return True

    # Signal 2 — meta blob contains the name
    blob = " ".join(
        [
            meta.get("title", ""),
            meta.get("og_title", ""),
            meta.get("og_description", ""),
        ]
    ).lower()
    if blob:
        meta_hits = sum(1 for p in name_parts if p in blob)
        if meta_hits / len(name_parts) >= 0.5:
            return True

    return False
