"""Prompt for and persist the NVIDIA / Apify keys used by the agent runner.

Reuses config.py's load/save so all keys live in the same .aienrich_config.json.
Env-var overrides (NVIDIA_API_KEY, APIFY_TOKEN) take precedence at read time.
A .env file in the working directory is also supported — see load_dotenv().
"""

from __future__ import annotations

import os

import config


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Lines starting with # are ignored. Surrounding single/double quotes
    on values are stripped. Existing environment variables are NOT
    overwritten — real shell env always wins over .env.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                if key and key not in os.environ and value and "REPLACE_ME" not in value:
                    os.environ[key] = value
    except OSError:
        pass


def _ask(label: str, default: str = "", secret: bool = False) -> str:
    if default:
        masked = _mask(default) if secret else default
        suffix = f" [{masked}]"
    else:
        suffix = ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        raise SystemExit(
            "ERROR: stdin closed during agent config prompt. "
            "Run interactively first or set NVIDIA_API_KEY / APIFY_TOKEN env vars."
        )
    return value or default


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def prompt_for_agent_keys(
    cfg: dict[str, str],
    *,
    reconfigure: bool = False,
) -> dict[str, str]:
    if reconfigure:
        print("Reconfiguring agent keys. Press Enter to keep the existing value.")

    if reconfigure or not cfg.get("nvidia_api_key"):
        cfg["nvidia_api_key"] = _ask(
            "NVIDIA API key (from build.nvidia.com)",
            cfg.get("nvidia_api_key", ""),
            secret=True,
        )

    if reconfigure or not cfg.get("apify_token"):
        cfg["apify_token"] = _ask(
            "Apify API token (from console.apify.com — leave blank to skip search)",
            cfg.get("apify_token", ""),
            secret=True,
        )

    config.save(cfg)
    return cfg


def resolve_keys(cfg: dict[str, str]) -> tuple[str, str]:
    nvidia = os.getenv("NVIDIA_API_KEY") or cfg.get("nvidia_api_key", "")
    apify = os.getenv("APIFY_TOKEN") or cfg.get("apify_token", "")
    return nvidia, apify
