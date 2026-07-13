"""Persistent usage stats for the enrichment bot (.aienrich_stats.json).

Per-provider search counts reset at the start of each calendar month (to match
free-tier monthly limits). All-time totals persist across months.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

STATS_FILE = ".aienrich_stats.json"


def _load() -> dict:
    p = Path(STATS_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


class Stats:
    def __init__(self) -> None:
        d = _load()
        month = date.today().strftime("%Y-%m")
        if d.get("month") != month:
            # New month: reset per-provider counters, keep lifetime totals.
            d = {"month": month, "providers": {}, "totals": d.get("totals", {})}
        d.setdefault("providers", {})
        totals = d.get("totals") or {}
        for k in ("urls", "ok", "error", "linkedin", "website"):
            totals.setdefault(k, 0)
        d["totals"] = totals
        self.d = d
        self._save()

    def _save(self) -> None:
        Path(STATS_FILE).write_text(json.dumps(self.d, indent=2), encoding="utf-8")

    def record_search(self, provider: str) -> None:
        self.d["providers"].setdefault(provider, {"searches": 0})
        self.d["providers"][provider]["searches"] += 1
        self._save()

    def record_result(self, rec: dict) -> None:
        t = self.d["totals"]
        t["urls"] += 1
        if rec.get("status") == "ok":
            t["ok"] += 1
        else:
            t["error"] += 1
        if rec.get("linkedin") not in ("", "Not found", None):
            t["linkedin"] += 1
        if rec.get("website") not in ("", "Not found", None):
            t["website"] += 1
        self._save()

    def summary(self) -> str:
        t = self.d["totals"]
        lines = [
            f"📊 Stats (searches reset monthly — {self.d['month']})",
            "",
            f"URLs processed (all-time): {t['urls']}",
            f"  ✅ ok: {t['ok']}   ❌ error: {t['error']}",
            f"  🔗 LinkedIn found: {t['linkedin']}",
            f"  🌐 Website found: {t['website']}",
            "",
            "Searches this month:",
        ]
        provs = self.d["providers"]
        if provs:
            for name, v in sorted(provs.items()):
                lines.append(f"  {name}: {v.get('searches', 0)}")
        else:
            lines.append("  (none yet)")
        return "\n".join(lines)
