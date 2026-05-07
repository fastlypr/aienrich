"""Shared SSL context for outbound HTTPS calls.

On macOS with python.org Python the default trust store is empty, so naked
urlopen() over HTTPS fails with CERTIFICATE_VERIFY_FAILED. We try to import
certifi (a single pip dep) and build an ssl.SSLContext from its bundle.
Falls back to None on systems where certifi isn't installed (Linux servers
typically use the OS trust store and don't need this).

Each module that calls urlopen() should pass `context=SSL_CONTEXT` when it's
not None and the URL is HTTPS.
"""

from __future__ import annotations

import ssl


def _build_ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


SSL_CONTEXT = _build_ssl_context()
