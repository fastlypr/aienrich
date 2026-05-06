"""Persisted runtime config for the enricher.

Stored at .aienrich_config.json in the working directory. Holds the public
Google Sheet URL, the column name to read URLs from, the Notion integration
token, and the Notion database id. Saved with 0o600 permissions because it
contains a secret.

Env var overrides (applied at load time, not persisted):
    NOTION_TOKEN, NOTION_DB_ID, AIENRICH_SHEET_URL
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

CONFIG_FILE = ".aienrich_config.json"

ENV_OVERRIDES = {
    "notion_token": "NOTION_TOKEN",
    "notion_db_id": "NOTION_DB_ID",
    "sheet_url": "AIENRICH_SHEET_URL",
}


def _path() -> Path:
    return Path(CONFIG_FILE)


def load() -> dict[str, str]:
    path = _path()
    data: dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(k): str(v) for k, v in raw.items() if v}
        except json.JSONDecodeError:
            pass

    for key, env_name in ENV_OVERRIDES.items():
        env_val = os.getenv(env_name)
        if env_val:
            data[key] = env_val
    return data


def save(data: dict[str, str]) -> None:
    path = _path()
    persisted = {k: v for k, v in data.items() if v}
    path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{_mask(default) if secret else default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        raise SystemExit(
            "ERROR: stdin closed during config prompt. Run interactively first "
            "or set NOTION_TOKEN / NOTION_DB_ID / AIENRICH_SHEET_URL env vars."
        )
    return value or default


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def extract_notion_id(value: str) -> str:
    """Pull a 32-hex Notion id from a URL or raw id string."""
    if not value:
        return ""
    cleaned = value.replace("-", "")
    match = re.search(r"([0-9a-fA-F]{32})", cleaned)
    return match.group(1) if match else value.strip()


def prompt_for_missing(config: dict[str, str], reconfigure: bool = False) -> dict[str, str]:
    if reconfigure:
        print("Reconfiguring. Press Enter to keep the existing value.")
    elif not config.get("sheet_url") or not config.get("notion_token"):
        print("First-run setup. Saving to .aienrich_config.json (chmod 600).")

    if reconfigure or not config.get("sheet_url"):
        config["sheet_url"] = _prompt(
            "Public Google Sheet URL", config.get("sheet_url", "")
        )

    if reconfigure or not config.get("sheet_column"):
        config["sheet_column"] = _prompt(
            "Sheet column header that holds URLs",
            config.get("sheet_column", "URL"),
        )

    if reconfigure or not config.get("notion_token"):
        config["notion_token"] = _prompt(
            "Notion integration token (starts with ntn_ or secret_)",
            config.get("notion_token", ""),
            secret=True,
        )

    if reconfigure or (not config.get("notion_db_id") and not config.get("notion_parent_page_id")):
        existing_db = config.get("notion_db_id", "")
        db_input = _prompt(
            "Notion database URL or ID (leave blank to auto-create one)",
            existing_db,
        )
        if db_input:
            config["notion_db_id"] = extract_notion_id(db_input)
            config.pop("notion_parent_page_id", None)
        else:
            existing_parent = config.get("notion_parent_page_id", "")
            parent_input = _prompt(
                "Notion parent page URL or ID (database will be created here)",
                existing_parent,
            )
            if parent_input:
                config["notion_parent_page_id"] = extract_notion_id(parent_input)
                config.pop("notion_db_id", None)

    save(config)
    return config
