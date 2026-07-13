#!/usr/bin/env python3
"""Telegram control bot for the enrichment tool.

Private, single-owner bot (long-polling, stdlib only). Lets you:
  • paste URLs → enrich them (single engine or waterfall)
  • pull URLs from a public Google Sheet (auto-maps the URL column, asks if unsure)
  • download result files (saved in results/ with dated names + used/unused tags)
  • see stats (searches per engine, totals)
  • settings: add/remove provider API keys, switch NVIDIA model, choose search
    mode + waterfall order, toggle Notion

Setup: put TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS (comma-separated numeric
Telegram user IDs) in .env, then run:  python3 telegram_bot.py
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import agent_config
import config
import fetch_sheet
import results_store
import search_providers as sp
import url_utils
from agent_nvidia import NvidiaClient
from bot_pipeline import enrich
from bot_stats import Stats

import http_utils

NVIDIA_MODELS = [
    "mistralai/mistral-small-4-119b-2603",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-8b-instruct",
    "openai/gpt-oss-120b",
    "nvidia/llama-3.1-nemotron-70b-instruct",
]

_URL_RE = re.compile(r"https?://\S+", re.I)


# ---------------------------------------------------------------------------
# Telegram API (stdlib)
# ---------------------------------------------------------------------------

class Bot:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"

    def _call(self, method: str, params: dict) -> dict:
        data = urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                          for k, v in params.items()}).encode("utf-8")
        req = Request(f"{self.base}/{method}", data=data)
        kwargs = {"timeout": 65}
        if http_utils.SSL_CONTEXT is not None:
            kwargs["context"] = http_utils.SSL_CONTEXT
        with urlopen(req, **kwargs) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_updates(self, offset: int) -> list[dict]:
        try:
            r = self._call("getUpdates", {"offset": offset, "timeout": 50})
            return r.get("result", [])
        except Exception as exc:  # network hiccup — back off briefly
            print("getUpdates error:", exc, flush=True)
            time.sleep(3)
            return []

    def send(self, chat: int, text: str, keyboard: list | None = None) -> None:
        params = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self._call("sendMessage", params)
        except Exception as exc:
            print("send error:", exc, flush=True)

    def answer_cb(self, cb_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})
        except Exception:
            pass

    def send_document(self, chat: int, path: Path, caption: str = "") -> None:
        boundary = "----aienrich" + str(int(time.time() * 1000))
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())

        field("chat_id", str(chat))
        if caption:
            field("caption", caption)
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: text/csv\r\n\r\n".encode()
        )
        parts.append(path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = Request(f"{self.base}/sendDocument", data=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        kwargs = {"timeout": 60}
        if http_utils.SSL_CONTEXT is not None:
            kwargs["context"] = http_utils.SSL_CONTEXT
        try:
            urlopen(req, **kwargs)
        except Exception as exc:
            print("send_document error:", exc, flush=True)
            self.send(chat, f"Couldn't send file: {exc}")


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_menu() -> list:
    return [
        [{"text": "▶️ Run (paste URLs)", "callback_data": "m:run"}],
        [{"text": "📥 Download results", "callback_data": "m:dl"},
         {"text": "📊 Stats", "callback_data": "m:stats"}],
        [{"text": "⚙️ Settings", "callback_data": "m:set"}],
    ]


def settings_menu(cfg: dict) -> list:
    mode = cfg.get("search_mode", "single")
    single = cfg.get("single_provider", "exa")
    model = cfg.get("nvidia_model") or NVIDIA_MODELS[0]
    notion = "on" if cfg.get("notion_enabled") == "1" else "off"
    mode_label = f"waterfall" if mode == "waterfall" else f"single: {single}"
    return [
        [{"text": f"🔎 Search mode: {mode_label}", "callback_data": "s:mode"}],
        [{"text": f"🧠 NVIDIA model: {model.split('/')[-1]}", "callback_data": "s:model"}],
        [{"text": "🔑 Provider API keys", "callback_data": "s:keys"}],
        [{"text": f"🗂 Notion: {notion}", "callback_data": "s:notion"}],
        [{"text": "⬅️ Back", "callback_data": "m:home"}],
    ]


def keys_menu(cfg: dict) -> list:
    rows = []
    for name, p in sp.PROVIDERS.items():
        has = sp.is_configured(name, cfg)
        mark = "✅" if has else "➕"
        rows.append([
            {"text": f"{mark} {p['label']}", "callback_data": f"k:add:{name}"},
            {"text": "🗑" if has else " ", "callback_data": f"k:del:{name}"},
        ])
    rows.append([{"text": "⬅️ Back", "callback_data": "m:set"}])
    return rows


def mode_menu() -> list:
    rows = [[{"text": f"Single: {sp.PROVIDERS[n]['label']}", "callback_data": f"mode:single:{n}"}]
            for n in sp.PROVIDERS]
    rows.append([{"text": "Waterfall (all, in order)", "callback_data": "mode:waterfall:"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "m:set"}])
    return rows


def model_menu() -> list:
    rows = [[{"text": m.split("/")[-1], "callback_data": f"model:{i}"}]
            for i, m in enumerate(NVIDIA_MODELS)]
    rows.append([{"text": "⬅️ Back", "callback_data": "m:set"}])
    return rows


# ---------------------------------------------------------------------------
# Bot app
# ---------------------------------------------------------------------------

class App:
    def __init__(self, bot: Bot, allowed: set[int]) -> None:
        self.bot = bot
        self.allowed = allowed
        self.pending: dict[int, dict] = {}  # chat_id -> awaited-input state

    # -- helpers
    def cfg(self) -> dict:
        return config.load()

    def client(self, cfg: dict) -> NvidiaClient:
        key = cfg.get("nvidia_api_key") or os.getenv("NVIDIA_API_KEY", "")
        model = cfg.get("nvidia_model") or NVIDIA_MODELS[0]
        return NvidiaClient(api_key=key, model=model)

    def do_search_fn(self, cfg: dict, stats: Stats):
        if cfg.get("search_mode") == "waterfall":
            order = (cfg.get("waterfall_order") or ",".join(sp.DEFAULT_ORDER)).split(",")
            return lambda q: sp.waterfall([o for o in order if o], q, cfg, stats)
        prov = cfg.get("single_provider", "exa")
        return lambda q: (prov, sp.search(prov, q, cfg, stats))

    # -- run a batch of URLs
    def run_urls(self, chat: int, urls: list[str]) -> None:
        cfg = self.cfg()
        if not (cfg.get("nvidia_api_key") or os.getenv("NVIDIA_API_KEY")):
            self.bot.send(chat, "⚠️ No NVIDIA key set. Add it in Settings → Provider API keys first.")
            return
        active = [n for n in sp.PROVIDERS if sp.is_configured(n, cfg)]
        if not active:
            self.bot.send(chat, "⚠️ No search provider configured. Add a key in Settings first.")
            return

        stats = Stats()
        client = self.client(cfg)
        do_search = self.do_search_fn(cfg, stats)
        path, name = results_store.new_result_file()
        self.bot.send(chat, f"▶️ Running {len(urls)} URL(s) → “{name}”")

        notion = cfg.get("notion_enabled") == "1" and cfg.get("notion_token") and cfg.get("notion_db_id")
        nprops = None
        if notion:
            try:
                import notion_writer_exa
                nprops = notion_writer_exa.ensure_schema(cfg["notion_token"], cfg["notion_db_id"])
            except Exception as exc:
                self.bot.send(chat, f"(Notion off — schema error: {exc})")
                notion = False

        for i, url in enumerate(urls, 1):
            rec = enrich(url, client, do_search)
            results_store.append_row(path, name, rec)
            stats.record_result(rec)
            if notion:
                try:
                    import notion_writer_exa
                    notion_writer_exa.upsert(cfg["notion_token"], cfg["notion_db_id"], rec, nprops)
                except Exception:
                    pass
            if rec["status"] == "ok":
                line = (f"[{i}/{len(urls)}] ✅ {rec['name']} — {rec['company']}\n"
                        f"🔗 {rec['linkedin']}\n🌐 {rec['website']}\n🏷 {rec['category']}")
            else:
                line = f"[{i}/{len(urls)}] ❌ {url}\n{rec['error']}"
            self.bot.send(chat, line)

        self.bot.send(chat, f"✅ Done → “{name}” saved in results/.",
                      keyboard=[[{"text": "📥 Download this file", "callback_data": f"dlname:{name}"}]])

    # -- google sheet
    def handle_sheet(self, chat: int, sheet_url: str) -> None:
        try:
            headers = fetch_sheet.get_headers(sheet_url)
        except Exception as exc:
            self.bot.send(chat, f"Couldn't read that sheet (is it shared 'Anyone with the link'?):\n{exc}")
            return
        if not headers:
            self.bot.send(chat, "That sheet looks empty.")
            return
        col = fetch_sheet.guess_url_column(headers)
        if col:
            self.fetch_and_run_sheet(chat, sheet_url, col)
        else:
            self.pending[chat] = {"await": "sheet_col", "sheet_url": sheet_url, "headers": headers}
            rows = [[{"text": h, "callback_data": f"col:{i}"}] for i, h in enumerate(headers)]
            self.bot.send(chat, "Which column has the article URLs?", keyboard=rows)

    def fetch_and_run_sheet(self, chat: int, sheet_url: str, column: str) -> None:
        try:
            urls = fetch_sheet.fetch_urls(sheet_url, column)
        except Exception as exc:
            self.bot.send(chat, f"Couldn't read column “{column}”: {exc}")
            return
        if not urls:
            self.bot.send(chat, f"No URLs found in column “{column}”.")
            return
        self.bot.send(chat, f"Found {len(urls)} URL(s) in “{column}”.")
        self.run_urls(chat, urls)

    # -- dispatch
    def on_message(self, chat: int, text: str) -> None:
        text = (text or "").strip()

        # pending multi-step input?
        pend = self.pending.get(chat)
        if pend and pend.get("await") == "add_key":
            provider = pend["provider"]
            cfg = self.cfg()
            cfg[sp.PROVIDERS[provider]["cfg_key"]] = text
            config.save(cfg)
            self.pending.pop(chat, None)
            self.bot.send(chat, f"✅ Saved {sp.PROVIDERS[provider]['label']} key.",
                          keyboard=keys_menu(self.cfg()))
            return

        if text.startswith("/start") or text.startswith("/menu") or text.startswith("/help"):
            self.bot.send(chat, "🤖 Enrichment bot. Paste URLs to enrich, or use the menu.",
                          keyboard=main_menu())
            return
        if text.startswith("/stats"):
            self.bot.send(chat, Stats().summary(), keyboard=main_menu())
            return
        if text.startswith("/settings"):
            self.bot.send(chat, "⚙️ Settings", keyboard=settings_menu(self.cfg()))
            return
        if text.startswith("/sheet"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and "docs.google.com" in parts[1]:
                self.handle_sheet(chat, parts[1].strip())
            else:
                self.bot.send(chat, "Send: /sheet <public Google Sheet URL>")
            return

        # a google sheet URL pasted directly
        if "docs.google.com/spreadsheets" in text:
            self.handle_sheet(chat, _URL_RE.search(text).group(0))
            return

        # plain URLs → run
        urls, seen = [], set()
        for m in _URL_RE.findall(text):
            n = url_utils.normalize(m)
            if n and n not in seen:
                seen.add(n)
                urls.append(n)
        if urls:
            self.run_urls(chat, urls)
        else:
            self.bot.send(chat, "Send me one or more article URLs (or use the menu).",
                          keyboard=main_menu())

    def on_callback(self, chat: int, data: str, cb_id: str) -> None:
        self.bot.answer_cb(cb_id)
        cfg = self.cfg()

        if data == "m:home":
            self.bot.send(chat, "🏠 Menu", keyboard=main_menu()); return
        if data == "m:run":
            self.bot.send(chat, "Paste one or more URLs (one per line), or send /sheet <url>."); return
        if data == "m:stats":
            self.bot.send(chat, Stats().summary(), keyboard=main_menu()); return
        if data == "m:set":
            self.bot.send(chat, "⚙️ Settings", keyboard=settings_menu(cfg)); return
        if data == "m:dl":
            self.show_downloads(chat); return

        # settings submenus
        if data == "s:keys":
            self.bot.send(chat, "🔑 Tap a provider to add/replace its key; 🗑 to remove.",
                          keyboard=keys_menu(cfg)); return
        if data == "s:mode":
            self.bot.send(chat, "🔎 Choose search mode:", keyboard=mode_menu()); return
        if data == "s:model":
            self.bot.send(chat, "🧠 Choose NVIDIA model:", keyboard=model_menu()); return
        if data == "s:notion":
            cfg["notion_enabled"] = "0" if cfg.get("notion_enabled") == "1" else "1"
            config.save(cfg)
            self.bot.send(chat, "⚙️ Settings", keyboard=settings_menu(cfg)); return

        if data.startswith("k:add:"):
            prov = data.split(":")[2]
            self.pending[chat] = {"await": "add_key", "provider": prov}
            self.bot.send(chat, f"Send the API key for {sp.PROVIDERS[prov]['label']} (it's stored locally):")
            return
        if data.startswith("k:del:"):
            prov = data.split(":")[2]
            cfg.pop(sp.PROVIDERS[prov]["cfg_key"], None)
            config.save(cfg)
            self.bot.send(chat, f"Removed {sp.PROVIDERS[prov]['label']} key.", keyboard=keys_menu(cfg)); return

        if data.startswith("mode:"):
            _, kind, prov = data.split(":", 2)
            cfg["search_mode"] = "waterfall" if kind == "waterfall" else "single"
            if kind == "single":
                cfg["single_provider"] = prov
            config.save(cfg)
            self.bot.send(chat, "⚙️ Settings", keyboard=settings_menu(cfg)); return

        if data.startswith("model:"):
            idx = int(data.split(":")[1])
            cfg["nvidia_model"] = NVIDIA_MODELS[idx]
            config.save(cfg)
            self.bot.send(chat, "⚙️ Settings", keyboard=settings_menu(cfg)); return

        if data.startswith("col:"):
            pend = self.pending.pop(chat, None)
            if pend and pend.get("await") == "sheet_col":
                col = pend["headers"][int(data.split(":")[1])]
                self.fetch_and_run_sheet(chat, pend["sheet_url"], col)
            return

        if data.startswith("dl:"):  # download by index
            idx = int(data.split(":")[1])
            files = results_store.list_files()
            if 0 <= idx < len(files):
                self.send_result(chat, files[idx]["name"])
            return
        if data.startswith("dlname:"):
            self.send_result(chat, data.split(":", 1)[1]); return
        if data.startswith("use:"):
            _, idx, val = data.split(":")
            files = results_store.list_files()
            if 0 <= int(idx) < len(files):
                results_store.set_used(files[int(idx)]["name"], val == "1")
            self.show_downloads(chat); return

    def show_downloads(self, chat: int) -> None:
        files = results_store.list_files()
        if not files:
            self.bot.send(chat, "No result files yet — run some URLs first.", keyboard=main_menu()); return
        rows = []
        for i, e in enumerate(files[:20]):
            rows.append([{"text": f"📄 {e['name']} · {results_store.tag(e)} · {e.get('rows',0)} rows",
                          "callback_data": f"dl:{i}"}])
            toggle = "0" if e.get("used") else "1"
            label = "mark unused" if e.get("used") else "mark used"
            rows.append([{"text": f"   {label}", "callback_data": f"use:{i}:{toggle}"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "m:home"}])
        self.bot.send(chat, "📥 Your result files:", keyboard=rows)

    def send_result(self, chat: int, name: str) -> None:
        p = results_store.path_for(name)
        if p and p.exists():
            self.bot.send_document(chat, p, caption=name)
        else:
            self.bot.send(chat, "That file is missing.")

    # -- main loop
    def run(self) -> None:
        offset = 0
        print("Bot started. Waiting for messages…", flush=True)
        while True:
            for upd in self.bot.get_updates(offset):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                cb = upd.get("callback_query")
                user = (msg or cb or {}).get("from", {}).get("id")
                chat = ((msg or {}).get("chat", {}).get("id")
                        or (cb or {}).get("message", {}).get("chat", {}).get("id"))
                if chat is None:
                    continue
                print(f"update from user_id={user} chat_id={chat}", flush=True)
                if self.allowed and user not in self.allowed:
                    self.bot.send(chat, f"🔒 Not authorized. Ask the owner to add your ID: {user}")
                    continue
                try:
                    if cb:
                        self.on_callback(chat, cb.get("data", ""), cb["id"])
                    elif msg and "text" in msg:
                        self.on_message(chat, msg["text"])
                except Exception as exc:
                    print("handler error:", exc, flush=True)
                    self.bot.send(chat, f"⚠️ Error: {exc}")


def _acquire_singleton_lock() -> Path | None:
    """Refuse to start if another instance is already running (prevents the
    duplicate-replies problem from multiple pollers)."""
    lock = Path(".bot.lock")
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)  # raises if the pid is dead
            print(f"ERROR: bot already running (pid {pid}). Stop it first: "
                  f"pkill -f telegram_bot.py", flush=True)
            return None
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock — take it over
    lock.write_text(str(os.getpid()))
    return lock


def main() -> int:
    agent_config.load_dotenv()
    cfg = config.load()
    token = os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_bot_token", "")
    if not token:
        print("ERROR: set TELEGRAM_BOT_TOKEN in .env", flush=True)
        return 2

    lock = _acquire_singleton_lock()
    if lock is None:
        return 3
    import atexit
    atexit.register(lambda: lock.exists() and lock.unlink())
    raw = os.getenv("TELEGRAM_ALLOWED_USERS") or cfg.get("telegram_allowed_users", "")
    allowed = {int(x) for x in re.findall(r"\d+", raw)}
    if not allowed:
        print("WARNING: TELEGRAM_ALLOWED_USERS not set — the bot will tell each "
              "user their ID but refuse actions until you add it.", flush=True)
    App(Bot(token), allowed).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
