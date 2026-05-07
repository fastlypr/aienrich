"""Stage 2: extract person/company facts from article text via NVIDIA NIM."""

from __future__ import annotations

from agent_nvidia import NvidiaClient


_EXTRACT_PROMPT = """You are reading a news article and extracting facts about \
the main person featured.

Identify the main person — usually the interviewee, the founder/executive being \
profiled, or the named subject in the headline. Ignore quoted experts, \
journalists, photographers, and supporting names. If multiple people are \
co-featured equally, pick the one named first in the headline or byline.

Return a JSON object with exactly these fields:
{
  "name": "Full name (exact spelling from the article); empty string if no clear main person",
  "company": "Company or organization; empty string if none",
  "role": "Job title or role; empty string if none",
  "location": "City/region if mentioned; empty string if none",
  "industry": "Industry or field; empty string if unclear",
  "achievements": ["Notable achievement 1", "Notable achievement 2"]
}

Anti-hallucination rules:
- Use facts present in the article only. Do not infer or guess.
- If the article is about a company rather than a person, identify the founder \
or CEO if they are clearly the focus, otherwise return empty strings.

Return only valid JSON. No markdown fences. No commentary."""


def extract_facts(article_text: str, client: NvidiaClient) -> dict:
    prompt = f"{_EXTRACT_PROMPT}\n\nArticle text:\n{article_text}"
    data = client.chat_json(prompt, max_tokens=1024)

    achievements_raw = data.get("achievements") or []
    achievements: list[str] = []
    if isinstance(achievements_raw, list):
        for item in achievements_raw:
            if isinstance(item, (str, int, float)):
                s = str(item).strip()
                if s:
                    achievements.append(s)

    return {
        "name": str(data.get("name") or "").strip(),
        "company": str(data.get("company") or "").strip(),
        "role": str(data.get("role") or "").strip(),
        "location": str(data.get("location") or "").strip(),
        "industry": str(data.get("industry") or "").strip(),
        "achievements": achievements,
    }
