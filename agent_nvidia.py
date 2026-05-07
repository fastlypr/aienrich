"""NVIDIA NIM client wrapper.

Uses the OpenAI-compatible API at integrate.api.nvidia.com. Default model
is openai/gpt-oss-120b; override via the NVIDIA_MODEL env var.

Two convenience methods:
  chat()      -> str    (returns the message content)
  chat_json() -> dict   (parses the response as JSON, tolerates fences)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI


DEFAULT_MODEL = os.getenv("NVIDIA_MODEL", "openai/gpt-oss-120b")
BASE_URL = "https://integrate.api.nvidia.com/v1"


class NvidiaClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        if not api_key:
            raise ValueError("NVIDIA_API_KEY not set.")
        self._client = OpenAI(base_url=BASE_URL, api_key=api_key)
        self.model = model

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception:
            # Fallback if the model rejects response_format.
            if json_mode and "response_format" in kwargs:
                kwargs.pop("response_format", None)
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise

        return (resp.choices[0].message.content or "").strip()

    def chat_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> dict:
        text = self.chat(
            prompt,
            system=system,
            json_mode=True,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        return _parse_json_loose(text)


def _parse_json_loose(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])
