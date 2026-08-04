#!/usr/bin/env python3
"""
run_ig.py — Instagram DM personalizer.

Exactly the same pipeline as run_all.py (CSV or Google Sheet, concurrent,
rate-limited, resumable, in-place / OUT), but it uses the IG DM prompt
(prompt-ig-dm.skill.md) which produces a 1-3 word topic instead of the email
phrase. Your email setup (run_all.py + personalize-cold-email.skill.md) is
untouched.

Usage:
  python3 run_ig.py "leads.csv"
  python3 run_ig.py "https://docs.google.com/spreadsheets/d/<ID>/edit"

Tip: point OUT at a separate file so IG topics don't overwrite your email ones:
  OUT="results/ig_dm.csv" python3 run_ig.py "leads.csv"
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Use the IG DM prompt unless the caller explicitly set SKILL_FILE.
os.environ.setdefault("SKILL_FILE", os.path.join(SCRIPT_DIR, "prompt-ig-dm.skill.md"))

import run_all  # noqa: E402  — imported after SKILL_FILE is set so the child inherits it

if __name__ == "__main__":
    sys.exit(run_all.main())
