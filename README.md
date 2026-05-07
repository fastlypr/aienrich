# AI Enrich

Enrich article URLs with the LinkedIn profile, Instagram profile, and a 1–2 word
professional category of the main person featured. URLs come from a public
Google Sheet (or a local file), results are appended to `results.csv` and
upserted into a Notion database.

## Three runners

- **`enrich_urls_codex.py`** — uses the local **Codex CLI** (`codex exec`). One
  long-lived session is reused across URLs, so the skill rules are sent once
  and only re-sent if the model drifts or after every N URLs.
- **`enrich_urls_agent.py`** — multi-stage agent: fetch article → extract
  facts via NVIDIA NIM → Google search via Apify → rank candidates → verify
  → category. Deterministic, observable, and avoids Codex usage limits.
  Writes to `results_agent.csv` so you can A/B compare against the codex
  runner.
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
   | Name          | Title     |
   | Article URL   | URL       |
   | Company       | Rich text |
   | LinkedIn      | URL       |
   | Instagram     | URL       |
   | Category      | Select    |
   | Status        | Select    |
   | Error         | Rich text |

The title column can be named anything in your database — the script picks up
whatever it's called and treats it as the person-name field. Dedup uses the
`Article URL` property, not the title.

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

## Run as a systemd timer (Ubuntu)

The repo ships a oneshot service + timer that runs the enricher every 30
minutes. Add a URL to your sheet → next tick picks it up. Already-processed
URLs are skipped automatically.

Install once:

```bash
sudo cp systemd/aienrich.service /etc/systemd/system/
sudo cp systemd/aienrich.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aienrich.timer
```

Manage it:

```bash
# Trigger a run right now (ignores the schedule)
sudo systemctl start aienrich.service

# See when the next run is scheduled
systemctl list-timers aienrich.timer

# Watch live logs
journalctl -u aienrich.service -f

# Stop the recurring runs
sudo systemctl disable --now aienrich.timer
```

If `codex` isn't found when the service runs, edit
`/etc/systemd/system/aienrich.service`, fix the `PATH=` line so it includes
the directory containing the `codex` binary, then run
`sudo systemctl daemon-reload`. Find that directory with `which codex` from
your normal shell.

## Agent runner (NVIDIA + Apify)

Same Google Sheet, same Notion DB — different enrichment engine. Stages:

1. Fetch article HTML.
2. NVIDIA NIM extracts the main person, company, role, location.
3. Build site-restricted Google queries from those facts.
4. Apify Google Search Scraper runs the queries.
5. Heuristic + NIM picker chooses the best LinkedIn / Instagram URL.
6. Fetch og:meta on the chosen URLs and demote to "Not found" on mismatch.
7. NIM produces the 1–2 word category.

Setup:

```bash
python3 enrich_urls_agent.py --reconfigure
```

You'll be asked for the existing sheet/Notion settings plus:

- `NVIDIA API key` — from <https://build.nvidia.com> (free tier works).
- `Apify API token` — from <https://console.apify.com>. Leave blank to skip
  the LinkedIn/Instagram search step (the agent still extracts name,
  company, and category).

Then just run it:

```bash
python3 enrich_urls_agent.py            # all URLs
python3 enrich_urls_agent.py --limit 5  # smoke-test with 5 URLs
```

Override the model (default `openai/gpt-oss-120b`) with the `NVIDIA_MODEL`
env var.

## Env var overrides

These take precedence over `.aienrich_config.json` for the run, without being
written back:

- `NOTION_TOKEN`
- `NOTION_DB_ID`
- `AIENRICH_SHEET_URL`
- `NVIDIA_API_KEY` (agent runner only)
- `APIFY_TOKEN` (agent runner only)
- `NVIDIA_MODEL` (agent runner only — defaults to `openai/gpt-oss-120b`)

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
