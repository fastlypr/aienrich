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

import html
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
# Quiet the noisy HTTP client loggers from the OpenAI SDK / urllib3.
for _noisy in ("httpx", "httpcore", "openai", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("aienrich")

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
            log.warning("getUpdates error: %s", exc)
            time.sleep(3)
            return []

    def send(self, chat: int, text: str, keyboard: list | None = None,
             parse_mode: str | None = None) -> int | None:
        """Send a message. Returns its message_id so it can be edited later."""
        params = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            r = self._call("sendMessage", params)
            return (r.get("result") or {}).get("message_id")
        except Exception as exc:
            log.warning("send error: %s", exc)
            return None

    def edit(self, chat: int, message_id: int, text: str, keyboard: list | None = None,
             parse_mode: str | None = None) -> None:
        """Edit an existing message in place (used for the live progress card)."""
        params = {"chat_id": chat, "message_id": message_id, "text": text,
                  "disable_web_page_preview": True}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self._call("editMessageText", params)
        except Exception as exc:
            # "message is not modified" is harmless — same text re-sent.
            if "not modified" not in str(exc).lower():
                log.warning("edit error: %s", exc)

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
            log.warning("send_document error: %s", exc)
            self.send(chat, f"Couldn't send file: {exc}")


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_menu() -> list:
    return [
        [{"text": "▶️ Run (paste URLs)", "callback_data": "m:run"}],
        [{"text": "✍️ Personalizer", "callback_data": "m:pz"}],
        [{"text": "📥 Download results", "callback_data": "m:dl"},
         {"text": "📊 Stats", "callback_data": "m:stats"}],
        [{"text": "⚙️ Settings", "callback_data": "m:set"}],
    ]


def personalizer_menu() -> list:
    return [
        [{"text": "✉️ Cold Email topic", "callback_data": "pz:email"}],
        [{"text": "📸 Instagram DM topic", "callback_data": "pz:ig"}],
        [{"text": "⬅️ Back", "callback_data": "m:home"}],
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

    # -- progress card
    @staticmethod
    def _fmt_dur(sec: float) -> str:
        sec = int(sec)
        if sec < 60:
            return f"{sec}s"
        if sec < 3600:
            return f"{sec // 60}m {sec % 60:02d}s"
        return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"

    def _progress_text(self, name, done, total, ok, err, li, web, last, t0, finished=False):
        """HTML progress card. The stats block sits inside <pre> so the columns
        line up in Telegram's monospace font."""
        esc = html.escape
        pct = int(done / total * 100) if total else 0
        filled = round(pct / 100 * 18)
        bar = "█" * filled + "░" * (18 - filled)
        elapsed = time.time() - t0
        succ = (ok / done * 100) if done else 0.0
        li_pct = (li / ok * 100) if ok else 0.0
        web_pct = (web / ok * 100) if ok else 0.0

        head = ("✅ <b>Lead Scraping Complete</b>" if finished
                else "🚀 <b>Lead Scraping Progress</b>")

        # "Results Jul - 14" -> "Jul 14" for a tidier header
        disp = name.replace("Results ", "").replace(" - ", " ")

        body = [
            f"{'✅' if finished else '⏳'} {disp}",
            bar,
            f"{done} / {total} ({pct}%)",
            "",
            f"✅ Success       {ok}",
            f"❌ Failed        {err}",
            f"⚡ Success Rate  {succ:.1f}%",
            "",
            f"🔗 LinkedIn   {li} / {ok} ({li_pct:.0f}%)",
            f"🌐 Website    {web} / {ok} ({web_pct:.0f}%)",
            "",
            f"⏱ Elapsed     {self._fmt_dur(elapsed)}",
        ]
        if not finished and done:
            body.append(f"⌛ ETA         {self._fmt_dur((elapsed / done) * (total - done))}")

        text = f"{head}\n\n<pre>{esc(chr(10).join(body))}</pre>"

        if not finished and last and last.get("name") not in ("", "Not found", None):
            text += "\n━━━━━━━━━━━━━━━━━━\n<b>Current Lead</b>"
            text += f"\n👤 {esc(last['name'])}"
            co = last.get("company") or ""
            if co and co != "Not found":
                text += f"\n🏢 {esc(co)}"
        return text

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
        mode = ("waterfall" if cfg.get("search_mode") == "waterfall"
                else f"single:{cfg.get('single_provider', 'exa')}")
        model = (cfg.get("nvidia_model") or NVIDIA_MODELS[0]).split("/")[-1]
        log.info("▶ RUN started · %d URL(s) · mode=%s · model=%s · file=%r",
                 len(urls), mode, model, name)

        notion = cfg.get("notion_enabled") == "1" and cfg.get("notion_token") and cfg.get("notion_db_id")
        nprops = None
        if notion:
            try:
                import notion_writer_exa
                nprops = notion_writer_exa.ensure_schema(cfg["notion_token"], cfg["notion_db_id"])
            except Exception as exc:
                self.bot.send(chat, f"(Notion off — schema error: {exc})")
                notion = False

        total = len(urls)
        ok_n = err_n = li_n = web_n = 0
        t_start = time.time()
        recs: list[dict] = []
        # One live-updating progress card instead of a message per URL.
        msg_id = self.bot.send(
            chat, self._progress_text(name, 0, total, 0, 0, 0, 0, None, t_start),
            parse_mode="HTML")
        last_edit = 0.0

        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] %s", i, total, url)
            t0 = time.time()
            rec = enrich(url, client, do_search,
                         log=lambda m, i=i: log.info("   ├ [%d] %s", i, m))
            dt = time.time() - t0
            results_store.append_row(path, name, rec)
            stats.record_result(rec)
            recs.append(rec)

            if rec["status"] == "ok":
                ok_n += 1
                log.info("   └ [%d] ✅ done in %.1fs", i, dt)
            else:
                err_n += 1
                log.info("   └ [%d] ❌ %s (%.1fs)", i, rec["error"], dt)
            if rec["linkedin"] not in ("", "Not found"):
                li_n += 1
            if rec["website"] not in ("", "Not found"):
                web_n += 1

            if notion:
                try:
                    import notion_writer_exa
                    notion_writer_exa.upsert(cfg["notion_token"], cfg["notion_db_id"], rec, nprops)
                except Exception as exc:
                    log.warning("   ├ [%d] notion write failed: %s", i, exc)

            # Throttle edits so Telegram doesn't rate-limit us.
            now = time.time()
            if msg_id and (now - last_edit >= 2.0 or i == total):
                self.bot.edit(chat, msg_id, self._progress_text(
                    name, i, total, ok_n, err_n, li_n, web_n, rec, t_start),
                    parse_mode="HTML")
                last_edit = now

        log.info("✔ RUN done · %d ok · %d error(s) · file=%r", ok_n, err_n, name)
        final = self._progress_text(
            name, total, total, ok_n, err_n, li_n, web_n, None, t_start, finished=True)
        # For small runs, show the actual results inline — no need to open the CSV.
        if total <= 5:
            esc = html.escape
            for r in recs:
                if r["status"] == "ok":
                    final += (f"\n\n<b>{esc(r['name'])}</b> — {esc(r['company'])}"
                              f"\n{esc(r['linkedin'])}"
                              f"\n{esc(r['website'])}"
                              f"\n<i>{esc(r['category'])}</i>")
                else:
                    final += f"\n\n❌ {esc(r['url'])}\n<i>{esc(r['error'])}</i>"
        kb = [[{"text": "📥 Download CSV", "callback_data": f"dlname:{name}"}]]
        if msg_id:
            self.bot.edit(chat, msg_id, final, keyboard=kb, parse_mode="HTML")
        else:
            self.bot.send(chat, final, keyboard=kb, parse_mode="HTML")

    # -- personalizer (vendored cold-emails scripts, run as-is)
    def start_personalizer(self, chat: int, source: str, mode: str) -> None:
        """Map the article/name columns (ask if unsure), then run."""
        try:
            headers = fetch_sheet.get_headers(source)
        except Exception as exc:
            self.bot.send(chat, f"Couldn't read that sheet (shared 'Anyone with the link'?):\n{exc}")
            return
        if not headers:
            self.bot.send(chat, "That sheet looks empty."); return
        article_col = fetch_sheet.guess_url_column(headers)
        name_col = fetch_sheet.guess_name_column(headers) or ""
        username_col = next(
            (h for h in headers if h.strip().lower() in
             ("username", "handle", "instagram", "ig", "insta")), "")
        log.info("✍ pz headers=%s · article=%r · name=%r · username=%r",
                 headers, article_col, name_col, username_col)
        if article_col:
            self.run_personalizer(chat, source, mode, name_col, article_col, username_col)
        else:
            self.pending[chat] = {"await": "pz_col", "mode": mode, "source": source,
                                  "headers": headers, "name_col": name_col,
                                  "username_col": username_col}
            rows = [[{"text": h, "callback_data": f"pzcol:{i}"}] for i, h in enumerate(headers)]
            rows.append([{"text": "⬅️ Back", "callback_data": "m:pz"}])
            self.bot.send(chat, "Which column has the ARTICLE URL to fetch?", keyboard=rows)

    def run_personalizer(self, chat: int, source: str, mode: str,
                         name_col: str = "", article_col: str = "",
                         username_col: str = "") -> None:
        cfg = self.cfg()
        here = Path(__file__).resolve().parent
        script = here / "personalizer" / ("run_ig.py" if mode == "ig" else "run_all.py")
        if not script.exists():
            self.bot.send(chat, "Personalizer scripts not found (personalizer/)."); return

        Path("results").mkdir(exist_ok=True)
        label = "IG-DM" if mode == "ig" else "ColdEmail"
        base = f"results/Personalized {label} {date.today().strftime('%b-%d')}"
        out = f"{base}.csv"
        i = 2
        while Path(out).exists():
            out = f"{base} ({i}).csv"; i += 1

        env = dict(os.environ)
        env["OUT"] = out
        env["NVIDIA_API_KEY"] = cfg.get("nvidia_api_key") or os.getenv("NVIDIA_API_KEY", "")
        if cfg.get("nvidia_model"):
            env["NVIDIA_MODEL"] = cfg["nvidia_model"]
        if article_col:
            env["ARTICLE_COL"] = article_col
        if name_col:
            env["NAME_COL"] = name_col
        if username_col:
            env["USERNAME_COL"] = username_col
        env.setdefault("RPM", "38")
        env.setdefault("CONCURRENCY", "5")
        env["PYTHONUNBUFFERED"] = "1"  # stream child prints live (not block-buffered)

        kind = "Instagram DM" if mode == "ig" else "Cold Email"
        log.info("✍ personalizer start · %s · %s → %s", kind, source[:60], out)
        msg_id = self.bot.send(chat, f"✍️ Personalizing ({kind})…")
        total = ok = review = failed = done = 0
        t0 = time.time()
        last_edit = 0.0

        def card(finished=False):
            head = "✅ Personalization Complete" if finished else "✍️ Personalizing"
            pct = int(done / total * 100) if total else 0
            body = [f"{kind}", f"{done} / {total or '?'} ({pct}%)", "",
                    f"✅ ok      {ok}", f"⏭ skipped {review}", f"❌ failed  {failed}",
                    f"⏱ {self._fmt_dur(time.time() - t0)}"]
            return f"<b>{head}</b>\n\n<pre>{html.escape(chr(10).join(body))}</pre>"

        proc = subprocess.Popen([sys.executable, "-u", str(script), source],
                                cwd=str(here), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        tail: list[str] = []
        for line in proc.stdout:
            line = line.rstrip()
            log.info("   ✍ %s", line)
            if line:
                tail.append(line)
                tail = tail[-6:]
            m = re.search(r"Processing (\d+) leads", line)
            if m:
                total = int(m.group(1))
            m = re.match(r"\[(\d+)/(\d+)\]\s+(OK|SKIP|FAIL)", line)
            if m:
                done = int(m.group(1)); total = int(m.group(2))
                tag = m.group(3)
                ok += tag == "OK"; review += tag == "SKIP"; failed += tag == "FAIL"
            now = time.time()
            if msg_id and now - last_edit >= 2.0:
                self.bot.edit(chat, msg_id, card(), parse_mode="HTML")
                last_edit = now
        proc.wait()

        log.info("✍ personalizer done · ok=%d review=%d failed=%d", ok, review, failed)
        kb = [[{"text": "📥 Download CSV", "callback_data": "pzdl"}]]
        self._pz_last = out  # remember for the download button
        if msg_id:
            self.bot.edit(chat, msg_id, card(finished=True), keyboard=kb, parse_mode="HTML")
        if Path(out).exists():
            self.bot.send_document(chat, Path(out), caption=Path(out).name)
        else:
            why = "\n".join(tail[-4:]) or "no output"
            self.bot.send(chat, f"No output file produced. Last output:\n<pre>{html.escape(why)}</pre>",
                          parse_mode="HTML")

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
        log.info("📄 sheet headers=%s · guessed=%r", headers, col)
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
        log.info("📄 sheet import · column=%r · %d URL(s)", column, len(urls))
        self.bot.send(chat, f"Found {len(urls)} URL(s) in “{column}”.")
        self.run_urls(chat, urls)

    # -- dispatch
    def on_message(self, chat: int, text: str) -> None:
        text = (text or "").strip()

        # pending multi-step input?
        pend = self.pending.get(chat)
        if pend and pend.get("await") == "pz_input":
            self.pending.pop(chat, None)
            url = _URL_RE.search(text)
            if not url or "docs.google.com" not in text:
                self.bot.send(chat, "Send a public Google Sheet URL."); return
            self.start_personalizer(chat, url.group(0), pend["mode"]); return
        if pend and pend.get("await") == "add_key":
            provider = pend["provider"]
            cfg = self.cfg()
            cfg[sp.PROVIDERS[provider]["cfg_key"]] = text
            config.save(cfg)
            self.pending.pop(chat, None)
            log.info("⚙ key saved: %s", provider)
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

    def on_callback(self, chat: int, data: str, cb_id: str, msg_id: int | None = None) -> None:
        self.bot.answer_cb(cb_id)
        cfg = self.cfg()

        # nav() edits the SAME message the button is on (no new-message spam);
        # falls back to a fresh send if we somehow lack the message id.
        def nav(text: str, kb: list | None = None) -> None:
            if msg_id:
                self.bot.edit(chat, msg_id, text, keyboard=kb)
            else:
                self.bot.send(chat, text, keyboard=kb)

        if data == "m:home":
            nav("🏠 Menu", main_menu()); return
        if data == "m:run":
            nav("Paste one or more URLs (one per line), or send /sheet <url>.",
                [[{"text": "⬅️ Back", "callback_data": "m:home"}]]); return
        if data == "m:stats":
            nav(Stats().summary(), [[{"text": "⬅️ Back", "callback_data": "m:home"}]]); return
        if data == "m:set":
            nav("⚙️ Settings", settings_menu(cfg)); return
        if data == "m:dl":
            self.show_downloads(chat, msg_id); return
        if data == "m:pz":
            nav("✍️ Personalizer — pick a mode:", personalizer_menu()); return
        if data in ("pz:email", "pz:ig"):
            mode = "ig" if data == "pz:ig" else "email"
            self.pending[chat] = {"await": "pz_input", "mode": mode}
            kind = "Instagram DM" if mode == "ig" else "Cold Email"
            nav(f"{kind} selected.\nSend a public Google Sheet URL with your leads "
                f"(needs name + article URL columns; email carried through).",
                [[{"text": "⬅️ Back", "callback_data": "m:pz"}]])
            return
        if data.startswith("pzcol:"):
            pend = self.pending.pop(chat, None)
            if pend and pend.get("await") == "pz_col":
                article_col = pend["headers"][int(data.split(":")[1])]
                self.run_personalizer(chat, pend["source"], pend["mode"],
                                      pend.get("name_col", ""), article_col,
                                      pend.get("username_col", ""))
            return
        if data == "pzdl":
            out = getattr(self, "_pz_last", None)
            if out and Path(out).exists():
                self.bot.send_document(chat, Path(out), caption=Path(out).name)
            else:
                self.bot.send(chat, "No personalized file available.")
            return

        # settings submenus
        if data == "s:keys":
            nav("🔑 Tap a provider to add/replace its key; 🗑 to remove.", keys_menu(cfg)); return
        if data == "s:mode":
            nav("🔎 Choose search mode:", mode_menu()); return
        if data == "s:model":
            nav("🧠 Choose NVIDIA model:", model_menu()); return
        if data == "s:notion":
            cfg["notion_enabled"] = "0" if cfg.get("notion_enabled") == "1" else "1"
            config.save(cfg)
            nav("⚙️ Settings", settings_menu(cfg)); return

        if data.startswith("k:add:"):
            prov = data.split(":")[2]
            self.pending[chat] = {"await": "add_key", "provider": prov}
            nav(f"Send the API key for {sp.PROVIDERS[prov]['label']} (stored locally).\n"
                f"It replaces the current one.",
                [[{"text": "⬅️ Cancel", "callback_data": "s:keys"}]])
            return
        if data.startswith("k:del:"):
            prov = data.split(":")[2]
            cfg.pop(sp.PROVIDERS[prov]["cfg_key"], None)
            config.save(cfg)
            log.info("⚙ key removed: %s", prov)
            nav("🔑 Tap a provider to add/replace its key; 🗑 to remove.", keys_menu(cfg)); return

        if data.startswith("mode:"):
            _, kind, prov = data.split(":", 2)
            cfg["search_mode"] = "waterfall" if kind == "waterfall" else "single"
            if kind == "single":
                cfg["single_provider"] = prov
            config.save(cfg)
            log.info("⚙ search mode → %s", "waterfall" if kind == "waterfall" else f"single:{prov}")
            nav("⚙️ Settings", settings_menu(cfg)); return

        if data.startswith("model:"):
            idx = int(data.split(":")[1])
            cfg["nvidia_model"] = NVIDIA_MODELS[idx]
            config.save(cfg)
            log.info("⚙ model → %s", NVIDIA_MODELS[idx])
            nav("⚙️ Settings", settings_menu(cfg)); return

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
            self.show_downloads(chat, msg_id); return

    def show_downloads(self, chat: int, msg_id: int | None = None) -> None:
        def out(text, kb):
            if msg_id:
                self.bot.edit(chat, msg_id, text, keyboard=kb)
            else:
                self.bot.send(chat, text, keyboard=kb)
        files = results_store.list_files()
        if not files:
            out("No result files yet — run some URLs first.", main_menu()); return
        rows = []
        for i, e in enumerate(files[:20]):
            rows.append([{"text": f"📄 {e['name']} · {results_store.tag(e)} · {e.get('rows',0)} rows",
                          "callback_data": f"dl:{i}"}])
            toggle = "0" if e.get("used") else "1"
            label = "mark unused" if e.get("used") else "mark used"
            rows.append([{"text": f"   {label}", "callback_data": f"use:{i}:{toggle}"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "m:home"}])
        out("📥 Your result files:", rows)

    def send_result(self, chat: int, name: str) -> None:
        p = results_store.path_for(name)
        if p and p.exists():
            self.bot.send_document(chat, p, caption=name)
        else:
            self.bot.send(chat, "That file is missing.")

    # -- main loop
    def run(self) -> None:
        offset = 0
        log.info("Bot started. Waiting for messages…")
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
                if self.allowed and user not in self.allowed:
                    log.info("◀ blocked user_id=%s", user)
                    self.bot.send(chat, f"🔒 Not authorized. Ask the owner to add your ID: {user}")
                    continue
                try:
                    if cb:
                        log.info("◀ button %r from user_id=%s", cb.get("data", ""), user)
                        cb_msg_id = (cb.get("message") or {}).get("message_id")
                        self.on_callback(chat, cb.get("data", ""), cb["id"], cb_msg_id)
                    elif msg and "text" in msg:
                        preview = msg["text"].replace("\n", " ")[:60]
                        log.info("◀ message %r from user_id=%s", preview, user)
                        self.on_message(chat, msg["text"])
                except Exception as exc:
                    log.error("handler error: %s", exc)
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
