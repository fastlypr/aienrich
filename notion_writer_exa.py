"""Upsert Exa-workflow results into Notion — Website column instead of Instagram.

Reuses notion_writer's HTTP primitives (_request, find_page_by_url,
fetch_database, _url_property, _text_property, _alternate_name) so the shared
Apify notion_writer.py is not modified. The two writers can target the same
database: each ensure_schema only ADDS its missing columns, so a DB can carry
both Instagram (Apify) and Website (Exa) columns side by side.

Schema:
    <title>      - title       (person's full name)
    Article URL  - url
    Company      - rich_text
    LinkedIn     - url
    Website      - url
    Category     - select
    Status       - select  (ok | error)
    Error        - rich_text
"""

from __future__ import annotations

from typing import Any

import notion_writer
from notion_writer import BASE


_NON_TITLE_DEFAULTS: list[tuple[str, str, dict[str, Any]]] = [
    ("article_url", "Article URL", {"url": {}}),
    ("company", "Company", {"rich_text": {}}),
    ("linkedin", "LinkedIn", {"url": {}}),
    ("website", "Website", {"url": {}}),
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


def ensure_schema(token: str, db_id: str) -> dict[str, str]:
    """Adapt to the database's actual schema, adding any missing Exa columns.

    Mirrors notion_writer.ensure_schema but with the Website column.
    """
    db = notion_writer.fetch_database(token, db_id)
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
            alt = notion_writer._alternate_name(default_name)
            names[canonical] = alt
            to_add[alt] = schema
        else:
            names[canonical] = default_name
            to_add[default_name] = schema

    if to_add:
        notion_writer._request(
            "PATCH", f"{BASE}/databases/{db_id}", token, {"properties": to_add}
        )

    return names


def _build_properties(row: dict[str, str], names: dict[str, str]) -> dict[str, Any]:
    person_name = (row.get("name") or "").strip() or "Not found"
    error_text = row.get("error", "") or ""
    return {
        names["title"]: {
            "title": [{"type": "text", "text": {"content": person_name}}]
        },
        names["article_url"]: notion_writer._url_property(row.get("url", "")),
        names["company"]: notion_writer._text_property(row.get("company", "")),
        names["linkedin"]: notion_writer._url_property(row.get("linkedin", "")),
        names["website"]: notion_writer._url_property(row.get("website", "")),
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
    page_id = notion_writer.find_page_by_url(token, db_id, article_url, names)
    properties = _build_properties(row, names)
    if page_id:
        notion_writer._request(
            "PATCH", f"{BASE}/pages/{page_id}", token, {"properties": properties}
        )
        return page_id
    body = {"parent": {"database_id": db_id}, "properties": properties}
    result = notion_writer._request("POST", f"{BASE}/pages", token, body)
    return result["id"]
