"""Upsert enrichment results into a Notion database via the REST API.

Stdlib-only (urllib + json). One row per article URL — query by title to
update if it already exists, else create a new page.

Database schema (created automatically if needed):
    Article URL  - title
    LinkedIn     - url
    Instagram    - url
    Category     - select
    Status       - select  (ok | error)
    Error        - rich_text
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API_VERSION = "2022-06-28"
BASE = "https://api.notion.com/v1"


def _request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": API_VERSION,
        "Content-Type": "application/json",
    }
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=payload, method=method, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Notion API {exc.code}: {detail}") from exc


_REQUIRED_NON_TITLE = {
    "LinkedIn": {"url": {}},
    "Instagram": {"url": {}},
    "Category": {"select": {}},
    "Status": {
        "select": {
            "options": [
                {"name": "ok", "color": "green"},
                {"name": "error", "color": "red"},
            ]
        }
    },
    "Error": {"rich_text": {}},
}


def create_database(token: str, parent_page_id: str, title: str = "AI Enrich") -> str:
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": {"Article URL": {"title": {}}, **_REQUIRED_NON_TITLE},
    }
    result = _request("POST", f"{BASE}/databases", token, body)
    return result["id"]


def fetch_database(token: str, db_id: str) -> dict:
    return _request("GET", f"{BASE}/databases/{db_id}", token)


def ensure_schema(token: str, db_id: str) -> dict[str, str]:
    """Adapt to whatever schema the database has.

    Detects the existing title property's name (every DB has exactly one).
    Adds any of LinkedIn / Instagram / Category / Status / Error that are
    missing. Returns a map from canonical key -> actual Notion property name.
    """
    db = fetch_database(token, db_id)
    existing = db.get("properties", {})

    title_name: str | None = None
    for name, prop in existing.items():
        if prop.get("type") == "title":
            title_name = name
            break
    if title_name is None:
        raise RuntimeError(f"Database {db_id} has no title property.")

    to_add = {
        name: schema
        for name, schema in _REQUIRED_NON_TITLE.items()
        if name not in existing
    }
    if to_add:
        _request("PATCH", f"{BASE}/databases/{db_id}", token, {"properties": to_add})

    return {
        "title": title_name,
        "linkedin": "LinkedIn",
        "instagram": "Instagram",
        "category": "Category",
        "status": "Status",
        "error": "Error",
    }


def find_page_by_url(token: str, db_id: str, article_url: str, title_prop: str) -> str | None:
    body = {
        "filter": {"property": title_prop, "title": {"equals": article_url}},
        "page_size": 1,
    }
    result = _request("POST", f"{BASE}/databases/{db_id}/query", token, body)
    pages = result.get("results", [])
    return pages[0]["id"] if pages else None


def _url_property(value: str) -> dict[str, Any]:
    if value and value.lower().startswith(("http://", "https://")):
        return {"url": value}
    return {"url": None}


def _build_properties(row: dict[str, str], names: dict[str, str]) -> dict[str, Any]:
    error_text = row.get("error", "") or ""
    return {
        names["title"]: {
            "title": [{"type": "text", "text": {"content": row.get("url", "")}}]
        },
        names["linkedin"]: _url_property(row.get("linkedin", "")),
        names["instagram"]: _url_property(row.get("instagram", "")),
        names["category"]: {"select": {"name": row.get("category") or "public figure"}},
        names["status"]: {"select": {"name": row.get("status") or "ok"}},
        names["error"]: {
            "rich_text": (
                [{"type": "text", "text": {"content": error_text}}] if error_text else []
            )
        },
    }


def upsert(token: str, db_id: str, row: dict[str, str], names: dict[str, str]) -> str:
    article_url = row.get("url", "")
    page_id = find_page_by_url(token, db_id, article_url, names["title"])
    properties = _build_properties(row, names)
    if page_id:
        _request("PATCH", f"{BASE}/pages/{page_id}", token, {"properties": properties})
        return page_id
    body = {"parent": {"database_id": db_id}, "properties": properties}
    result = _request("POST", f"{BASE}/pages", token, body)
    return result["id"]
