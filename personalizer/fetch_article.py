#!/usr/bin/env python3
"""Thin CLI wrapper around agent_fetch.fetch_article_text().

agent_fetch.py is a library (no __main__), so this exposes it to the bash
pipeline. Give it a URL; it prints the cleaned article text to stdout.

    python3 fetch_article.py "https://example.com/post"

Exit codes (so the caller can tell apart kinds of failure):
    0  success, non-empty article text printed to stdout
   64  bad usage (missing/empty URL argument)
    2  fetch/parse error (network, HTTP, SSL, bad URL, ...)
    3  fetched OK but content is too short to be a real article

NOTE: this must run from a directory where BOTH agent_fetch.py and its
dependency http_utils.py are importable (i.e. your aienrich project dir on
the VM). Errors go to stderr; only the article text goes to stdout.
"""

import sys

# Minimum length to treat the result as a usable article. Below this we assume
# the fetch hit a paywall / JS-only page / error page and report failure so the
# caller can route the lead to review instead of spending tokens on opencode.
MIN_CHARS = 200


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        sys.stderr.write("usage: fetch_article.py <url>\n")
        return 64

    url = sys.argv[1].strip()

    try:
        from agent_fetch import fetch_article_text
        text = fetch_article_text(url)
    except Exception as exc:  # network, SSL, parse, import, etc.
        sys.stderr.write(f"fetch error for {url}: {exc}\n")
        return 2

    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        sys.stderr.write(f"content too short for {url} ({len(text)} chars)\n")
        return 3

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
