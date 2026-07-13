"""Manage the results/ folder: dated result files + a used/unused manifest.

Files are named like "Results Jul - 13.csv" (with " (2)", " (3)" … if that name
already exists on the same day). A small manifest tracks each file's row count
and whether you've marked it "used". New files start unused (shown as 🆕).
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

RESULTS_DIR = Path("results")
MANIFEST = RESULTS_DIR / ".manifest.json"

FIELDS = ["url", "name", "company", "linkedin", "website", "category", "status", "error"]


def _load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"files": []}


def _save_manifest(m: dict) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


def new_result_file(label: str | None = None) -> tuple[Path, str]:
    """Create a fresh dated CSV (header only) and register it. Returns (path, name)."""
    RESULTS_DIR.mkdir(exist_ok=True)
    base = label or f"Results {date.today().strftime('%b - %d')}"
    name = base
    i = 2
    while (RESULTS_DIR / f"{name}.csv").exists():
        name = f"{base} ({i})"
        i += 1
    path = RESULTS_DIR / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()
    m = _load_manifest()
    m["files"].append({
        "name": name,
        "file": path.name,
        "created": date.today().isoformat(),
        "rows": 0,
        "used": False,
    })
    _save_manifest(m)
    return path, name


def append_row(path: Path, name: str, rec: dict) -> None:
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow({k: rec.get(k, "") for k in FIELDS})
        f.flush()
    m = _load_manifest()
    for e in m["files"]:
        if e["name"] == name:
            e["rows"] = e.get("rows", 0) + 1
    _save_manifest(m)


def list_files() -> list[dict]:
    """Newest first."""
    return list(reversed(_load_manifest()["files"]))


def path_for(name: str) -> Path | None:
    for e in _load_manifest()["files"]:
        if e["name"] == name:
            return RESULTS_DIR / e["file"]
    return None


def set_used(name: str, used: bool = True) -> bool:
    m = _load_manifest()
    found = False
    for e in m["files"]:
        if e["name"] == name:
            e["used"] = used
            found = True
    _save_manifest(m)
    return found


def tag(entry: dict) -> str:
    return "✅ used" if entry.get("used") else "🆕 new"
