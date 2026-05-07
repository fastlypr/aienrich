"""Stage 1: fetch the article URL and extract clean reading text.

Stdlib-only. Strategy:
  1. urllib GET with a real User-Agent and gzip support.
  2. Parse with html.parser, strip script/style/nav/footer blocks.
  3. Prefer text inside <article>/<main>; fall back to body text.
"""

from __future__ import annotations

import gzip
import re
from html.parser import HTMLParser
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (compatible; aienrich/1.0; +https://github.com/fastlypr/aienrich)"
)


def fetch_html(url: str, timeout: int = 30) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        charset = resp.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


class _TextExtractor(HTMLParser):
    SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "form",
        "svg",
        "aside",
        "iframe",
    }
    BLOCK_TAGS = {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "br",
        "div",
        "article",
        "section",
    }
    ARTICLE_TAGS = {"article", "main"}

    def __init__(self) -> None:
        super().__init__()
        self.full_parts: list[str] = []
        self.article_parts: list[str] = []
        self._skip_depth = 0
        self._article_depth = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in self.ARTICLE_TAGS:
            self._article_depth += 1

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self.ARTICLE_TAGS and self._article_depth > 0:
            self._article_depth -= 1
        if tag in self.BLOCK_TAGS:
            target = self.article_parts if self._article_depth > 0 else None
            if target is not None:
                target.append("\n")
            self.full_parts.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth > 0:
            return
        text = data.strip()
        if not text:
            return
        if self._article_depth > 0:
            self.article_parts.append(text)
        self.full_parts.append(text)

    def text(self) -> str:
        article_text = " ".join(self.article_parts)
        article_text = re.sub(r"\s+", " ", article_text).strip()
        if len(article_text) >= 400:
            return article_text
        full_text = " ".join(self.full_parts)
        return re.sub(r"\s+", " ", full_text).strip()


# Section markers — everything from this point in the page is unrelated
# (recommended articles, footer, etc.) and gets truncated.
_TRUNCATE_MARKERS = (
    "More from Contributor Content",
    "More From Contributor Content",
    "More from contributor content",
    "More From",
    "Recommended Stories",
    "Recommended stories",
    "Related Stories",
    "Related stories",
    "You might also like",
    "Read more from",
    "Trending stories",
)

# Regex patterns scrubbed inline from the cleaned text. The byline pattern
# matches an exact "First Last Contributor" form to avoid eating title
# words. False positives are rare because article bodies don't normally
# contain "<Capitalized> <Capitalized> Contributor" sequences.
_BYLINE_RE = re.compile(
    r"\b[A-Z][a-z]+\s+[A-Z][a-zA-Z']+\s+Contributor\b"
)
_LABELS_RE = re.compile(
    r"\bCONTRIBUTOR\s+CONTENT\b|\bHear this story\b|\bSubscribe Now\b",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """Trim related-content tails and strip header noise / contributor bylines."""
    # Truncate at the earliest section marker we find.
    earliest = len(text)
    for marker in _TRUNCATE_MARKERS:
        idx = text.find(marker)
        if idx != -1 and idx < earliest:
            earliest = idx
    text = text[:earliest]

    # Drop labels and the "<First Last> Contributor" byline.
    text = _LABELS_RE.sub("", text)
    text = _BYLINE_RE.sub("", text)

    # Collapse whitespace and tidy up stray punctuation that leftover gaps make.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_article_text(html: str, max_chars: int = 12000) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # malformed HTML — keep whatever was collected
        pass
    text = _clean(parser.text())
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


def fetch_article_text(url: str, timeout: int = 30, max_chars: int = 12000) -> str:
    html = fetch_html(url, timeout=timeout)
    return extract_article_text(html, max_chars=max_chars)
