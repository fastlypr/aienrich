"""URL canonicalization for dedup.

Two URLs are treated as the same article if their normalized form matches.
Normalization:
  - adds https:// when the scheme is missing
  - lowercases the host and strips a leading www.
  - removes a trailing slash from the path (except root)
  - removes the fragment
  - drops common tracking query params (utm_*, gclid, fbclid, ref, mc_*, _ga)
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "gbraid",
    "wbraid",
    "fbclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "ref",
    "ref_src",
    "ref_url",
    "yclid",
}


def normalize(url: str) -> str:
    """Return a canonical string for dedup. Empty input -> empty string."""
    text = (url or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")

    parsed = urlparse(text)

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(kept)

    return urlunparse(("https", host, path, parsed.params, query, ""))
