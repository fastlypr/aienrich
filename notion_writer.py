"""Upsert enrichment results into a Notion database via the REST API.

Stdlib-only (urllib + json). One row per article URL — query by the article
URL property to find an existing page, then update or create.

Schema:
    <title>      - title       (holds the person's full name)
    Article URL  - url
    Company      - rich_text
    LinkedIn     - url
    Instagram    - url
    Category     - select
    Status       - select  (ok | error)
    Error        - rich_text

The title property's exact name varies — Notion lets the user rename it. The
script picks up whatever name the existing database uses (e.g. "Name",
"Title", "Person") and treats it as the name column.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API_VERSION = "2022-06-28"
BASE = "https://api.notion.com/v1"


_NON_TITLE_DEFAULTS: list[tuple[str, str, dict[str, Any]]] = [
    ("article_url", "Article URL", {"url": {}}),
    ("company", "Company", {"rich_text": {}}),
    ("linkedin", "LinkedIn", {"url": {}}),
    ("instagram", "Instagram", {"url": {}}),
    ("category", "Category", {"select": {}}),
    (
        "status",
        "Status",
        {
            "select": {
                "options": [
                    {"name": "ok", "color": "green"},
                    {"name": "error", "color": "red"},
                ]
            }
        },
    ),
    ("error", "Error", {"rich_text": {}}),
]


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


def create_database(token: str, parent_page_id: str, title: str = "AI Enrich") -> str:
    properties: dict[str, Any] = {"Name": {"title": {}}}
    for _canonical, default_name, schema in _NON_TITLE_DEFAULTS:
        properties[default_name] = schema
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": properties,
    }
    result = _request("POST", f"{BASE}/databases", token, body)
    return result["id"]


def fetch_database(token: str, db_id: str) -> dict:
    return _request("GET", f"{BASE}/databases/{db_id}", token)


def _alternate_name(default_name: str) -> str:
    return f"{default_name} (link)" if default_name == "Article URL" else f"{default_name} (text)"


def ensure_schema(token: str, db_id: str) -> dict[str, str]:
    """Adapt to the database's actual schema.

    Picks up the existing title property (whatever it's named) and treats it
    as the person-name column. Adds any missing non-title columns. Returns a
    map from canonical key -> actual Notion property name.
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

    names: dict[str, str] = {"title": title_name, "name": title_name}
    to_add: dict[str, dict[str, Any]] = {}

    for canonical, default_name, schema in _NON_TITLE_DEFAULTS:
        if default_name in existing:
            names[canonical] = default_name
            continue
        if default_name == title_name:
            alt = _alternate_name(default_name)
            names[canonical] = alt
            to_add[alt] = schema
        else:
            names[canonical] = default_name
            to_add[default_name] = schema

    if to_add:
        _request("PATCH", f"{BASE}/databases/{db_id}", token, {"properties": to_add})

    return names


def find_page_by_url(
    token: str, db_id: str, article_url: str, names: dict[str, str]
) -> str | None:
    body = {
        "filter": {
            "property": names["article_url"],
            "url": {"equals": article_url},
        },
        "page_size": 1,
    }
    result = _request("POST", f"{BASE}/databases/{db_id}/query", token, body)
    pages = result.get("results", [])
    return pages[0]["id"] if pages else None


def _url_property(value: str) -> dict[str, Any]:
    if value and value.lower().startswith(("http://", "https://")):
        return {"url": value}
    return {"url": None}


def _text_property(value: str) -> dict[str, Any]:
    text = (value or "").strip()
    if not text:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def _build_properties(row: dict[str, str], names: dict[str, str]) -> dict[str, Any]:
    person_name = (row.get("name") or "").strip() or "Not found"
    error_text = row.get("error", "") or ""
    return {
        names["title"]: {
            "title": [{"type": "text", "text": {"content": person_name}}]
        },
        names["article_url"]: _url_property(row.get("url", "")),
        names["company"]: _text_property(row.get("company", "")),
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
    page_id = find_page_by_url(token, db_id, article_url, names)
    properties = _build_properties(row, names)
    if page_id:
        _request("PATCH", f"{BASE}/pages/{page_id}", token, {"properties": properties})
        return page_id
    body = {"parent": {"database_id": db_id}, "properties": properties}
    result = _request("POST", f"{BASE}/pages", token, body)
    return result["id"]
