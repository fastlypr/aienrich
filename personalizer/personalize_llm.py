#!/usr/bin/env python3
"""
personalize_llm.py — call NVIDIA's hosted LLM to produce the cold-email
personalization JSON for ONE lead.

Drop-in replacement for the old `opencode run /personalize-cold-email ...` call
inside personalize.sh. It:
  - reads the skill text (the system prompt) from a .md file,
  - takes the lead fields as CLI args,
  - reads the cleaned article text from STDIN (avoids ARG_MAX limits),
  - calls the NVIDIA OpenAI-compatible endpoint (streaming),
  - prints ONLY the model's final reply (expected: one JSON object) to STDOUT.

The model runs with thinking enabled; its reasoning goes to the `reasoning_content`
stream and is discarded (or sent to STDERR if DEBUG_REASONING=1), so STDOUT stays
clean JSON for the bash side to parse.

Usage:
  python3 personalize_llm.py \
      --full_name "Dr. Eugene Lipov" \
      --article_url "https://..." \
      --linkedin "https://..." \
      --email "eugene@example.com" \
      < cleaned_article.txt

Environment:
  NVIDIA_API_KEY   (required)  your nvapi-... key
  NVIDIA_BASE_URL  default https://integrate.api.nvidia.com/v1
  NVIDIA_MODEL     default nvidia/nemotron-3-ultra-550b-a55b
  SKILL_FILE       default <script dir>/personalize-cold-email.skill.md
  LLM_TEMPERATURE  default 0.6   (lower = more consistent JSON)
  LLM_TOP_P        default 0.95
  LLM_MAX_TOKENS   default 16384 (also used as the reasoning budget on Nemotron)
  LLM_TIMEOUT      default 600   (seconds)
  LLM_REASONING_EFFORT  gpt-oss only: low|medium|high   (default high)
  LLM_EXTRA_BODY        advanced: raw JSON that overrides the per-model params
  LLM_MAX_RETRIES  retries on transient errors (rate/capacity); default 4
  LLM_RETRY_BACKOFF  base seconds for exponential backoff; default 2
  DEBUG_REASONING  if "1", stream the model's thinking to STDERR

Exit codes:
  0  success (JSON printed to stdout)
  1  API / network / empty-response error
  2  configuration error (missing key, missing skill file, openai not installed)
"""

import argparse
import json
import os
import random
import sys
import time


def die(msg, code=1):
    sys.stderr.write(f"personalize_llm: {msg}\n")
    sys.exit(code)


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return int(default)


def main():
    ap = argparse.ArgumentParser(description="NVIDIA cold-email personalizer (one lead).")
    ap.add_argument("--full_name", required=True)
    ap.add_argument("--article_url", default="")
    ap.add_argument("--linkedin", default="")
    ap.add_argument("--email", default="")
    args = ap.parse_args()

    api_key = (os.environ.get("NVIDIA_API_KEY") or "").strip()
    if not api_key:
        die("NVIDIA_API_KEY is not set in the environment.", 2)

    base_url = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    model = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
    temperature = env_float("LLM_TEMPERATURE", 0.6)
    top_p = env_float("LLM_TOP_P", 0.95)
    max_tokens = env_int("LLM_MAX_TOKENS", 16384)
    timeout = env_float("LLM_TIMEOUT", 600)
    debug_reasoning = os.environ.get("DEBUG_REASONING", "") == "1"

    # System prompt = the skill text. Default to the file next to this script.
    skill_file = os.environ.get("SKILL_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "personalize-cold-email.skill.md"
    )
    try:
        with open(skill_file, "r", encoding="utf-8") as fh:
            system_prompt = fh.read()
    except OSError as exc:
        die(f"could not read SKILL_FILE '{skill_file}': {exc}", 2)

    # The cleaned article text arrives on stdin (can be large).
    article_content = sys.stdin.read()

    user_msg = (
        f"full_name: {args.full_name}\n"
        f"article_url: {args.article_url}\n"
        f"linkedin: {args.linkedin}\n"
        f"email: {args.email}\n"
        f"article_content: {article_content}\n"
    )

    try:
        from openai import OpenAI
    except ImportError:
        die("the 'openai' package is not installed. Run: pip install openai", 2)

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    # Reasoning controls differ by model family. Nemotron uses an explicit
    # thinking toggle + budget; gpt-oss uses a reasoning_effort level. Other
    # models get a plain call. LLM_EXTRA_BODY (raw JSON) overrides all of this.
    model_lc = model.lower()
    extra_body = {}
    if "nemotron" in model_lc:
        if os.environ.get("LLM_THINKING", "1") == "1":
            extra_body = {
                "chat_template_kwargs": {"enable_thinking": True},
                "reasoning_budget": max_tokens,
            }
    elif "gpt-oss" in model_lc:
        extra_body = {"reasoning_effort": os.environ.get("LLM_REASONING_EFFORT", "high")}
    override = (os.environ.get("LLM_EXTRA_BODY") or "").strip()
    if override:
        try:
            extra_body = json.loads(override)
        except json.JSONDecodeError:
            die("LLM_EXTRA_BODY is not valid JSON.", 2)

    max_retries = env_int("LLM_MAX_RETRIES", 4)
    base_backoff = env_float("LLM_RETRY_BACKOFF", 2.0)
    # Transient conditions worth retrying (shared-tier capacity, rate limits, blips).
    transient = ("resourceexhausted", "request limit", "rate limit", "ratelimit",
                 "429", "too many requests", "503", "overloaded", "capacity",
                 "timeout", "temporarily", "unavailable")

    answer = ""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                extra_body=extra_body,
                stream=True,
            )
            parts = []
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning and debug_reasoning:
                    sys.stderr.write(reasoning)
                    sys.stderr.flush()
                content = getattr(delta, "content", None)
                if content:
                    parts.append(content)
            answer = "".join(parts).strip()
            if answer:
                break
            last_err = "empty completion (no content)"
            retryable = True
        except Exception as exc:  # noqa: BLE001 — surface/inspect any API/client error
            last_err = str(exc)
            retryable = any(m in last_err.lower() for m in transient)

        if attempt < max_retries and retryable:
            wait = min(base_backoff * (2 ** attempt), 15.0) + random.uniform(0, 1)
            sys.stderr.write(
                f"personalize_llm: transient error (attempt {attempt + 1}/{max_retries}), "
                f"retrying in {wait:.1f}s: {last_err[:120]}\n"
            )
            time.sleep(wait)
            continue
        die(f"NVIDIA API error after {attempt + 1} attempt(s): {last_err}", 1)

    if not answer:
        die(f"model returned no content: {last_err}", 1)

    # STDOUT must be just the model reply; personalize.sh extracts the JSON object.
    sys.stdout.write(answer)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
