# AI Enrich

Enrich article URLs with the LinkedIn profile, Instagram profile, and a 1–2 word
professional category of the main person featured. URLs come from a public
Google Sheet (or a local file), results are appended to `results.csv` and
upserted into a Notion database.

## Two runners

- **`enrich_urls_codex.py`** — uses the local **Codex CLI** (`codex exec`). One
  long-lived session is reused across URLs, so the skill rules are sent once
  and only re-sent if the model drifts or after every N URLs. **This is the
  one you probably want.**
- **`enrich_urls.py`** — uses the OpenAI Responses API directly (needs
  `OPENAI_API_KEY`). Same enrichment contract.

## Quick start (Codex runner)

```bash
git clone https://github.com/fastlypr/aienrich.git
cd aienrich
python3 enrich_urls_codex.py
```

The very first run prompts for:

1. Public Google Sheet URL (the sheet must be shared as "Anyone with the link")
2. Sheet column header that holds the URLs (default `URL`)
3. Notion integration token — create one at https://www.notion.so/my-integrations
4. Notion database URL (or a parent page URL — the script will create the DB
   for you)

Answers are saved to `.aienrich_config.json` (chmod 600). Subsequent runs skip
the prompts.

## Notion setup

1. Create an integration at https://www.notion.so/my-integrations and copy the
   secret.
2. In Notion, open the page where you want the database (or the database
   itself). Click `…` → **Connections** → add your integration. Without this,
   the API can't see the page.
3. Paste the page URL when prompted. The script creates the database with this
   schema:

   | Property      | Type      |
   | ------------- | --------- |
   | Article URL   | Title     |
   | LinkedIn      | URL       |
   | Instagram     | URL       |
   | Category      | Select    |
   | Status        | Select    |
   | Error         | Rich text |

Rows are upserted by article URL — re-running won't create duplicates.

## Useful flags

```bash
# Re-prompt for sheet / Notion settings
python3 enrich_urls_codex.py --reconfigure

# Skip Notion (still writes results.csv)
python3 enrich_urls_codex.py --no-notion

# Ignore the saved sheet and read urls.txt
python3 enrich_urls_codex.py --no-sheet --input urls.txt

# Start a fresh Codex session (drop the saved session id)
python3 enrich_urls_codex.py --fresh-session

# Rotate to a new Codex session every 25 URLs (default 50, 0 to disable)
python3 enrich_urls_codex.py --rotate-after 25

# Pacing
python3 enrich_urls_codex.py --batch-size 20 --delay 0.5 --batch-delay 10
```

## Env var overrides

These take precedence over `.aienrich_config.json` for the run, without being
written back:

- `NOTION_TOKEN`
- `NOTION_DB_ID`
- `AIENRICH_SHEET_URL`

## How the Codex session is reused

- First URL of a fresh session: full skill rules + URL.
- URLs 2…N: URL only — Codex remembers the rules from earlier turns.
- If the model returns something that doesn't parse as JSON, the script
  re-sends the rules with the URL once and retries.
- After `--rotate-after` URLs in the same session (default 50), a new session
  is started — capping context bloat and re-anchoring the rules.

## OpenAI runner (alternative)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="..."
python3 enrich_urls.py --input urls.txt --output results.csv
```

This script does **not** use the Google Sheet or Notion — it's a plain CSV
pipeline using the Responses API with `previous_response_id` for session
continuity.
