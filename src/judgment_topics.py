from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

import httpx
from bs4 import BeautifulSoup


_SEED_KEYWORDS: set[str] = {
    # Directly aligned with judgmentlabs.ai copy
    "agent reliability",
    "agent behavior monitoring",
    "agent behavior",
    "monitoring",
    "observability",
    "tracing",
    "traces",
    "anomaly detection",
    "anomalies",
    "evaluation",
    "scoring",
    "custom scoring",
    "golden dataset",
    "debugging",
    "production",
    "agent in production",
    "llm evaluation",
    "reliability",
    "safety",
    "security",
    "pii",
    "privacy",
    "hallucination",
    "prompting",
    "prompt optimization",
    "agent ops",
    "agentops",
    "observability for agents",
}


def _clean_text(s: str) -> str:
    s = re.sub(r"\\s+", " ", s)
    return s.strip()


def _extract_main_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # Remove obvious chrome
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    chunks: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        txt = _clean_text(el.get_text(" ", strip=True))
        if not txt:
            continue
        # Skip tiny nav fragments
        if len(txt) < 3:
            continue
        chunks.append(txt)
    return "\\n".join(chunks)


def fetch_site_text(url: str) -> str:
    headers = {
        "User-Agent": "TrackEventsBot/1.0 (research; +https://example.invalid)"
    }
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
    return _extract_main_text(r.text)


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\\s\\-_/]", " ", text)
    text = re.sub(r"\\s+", " ", text).strip()
    return [t for t in text.split(" ") if len(t) >= 3]


def _top_phrases(text: str, max_phrases: int = 40) -> set[str]:
    tokens = _tokenize(text)

    # Build 2-grams and 3-grams to capture phrases like "agent reliability"
    grams: Counter[str] = Counter()
    for n in (2, 3):
        for i in range(0, len(tokens) - n + 1):
            gram = " ".join(tokens[i : i + n])
            # Light filtering
            if any(x in gram for x in ("privacy policy", "terms of use")):
                continue
            grams[gram] += 1

    # Keep the most frequent, but also prefer those containing our core terms
    core = ("agent", "monitor", "observab", "trace", "score", "eval", "reliab", "anomal", "pii")
    ranked = sorted(
        grams.items(),
        key=lambda kv: (("agent" in kv[0]) or any(c in kv[0] for c in core), kv[1]),
        reverse=True,
    )
    return {g for g, _ in ranked[:max_phrases]}


def build_judgment_keyword_set(judgment_labs_url: str) -> set[str]:
    text = fetch_site_text(judgment_labs_url)
    phrases = _top_phrases(text)
    merged = set(_SEED_KEYWORDS)
    merged.update(phrases)
    # Normalize
    merged = {_clean_text(k.lower()) for k in merged if _clean_text(k)}
    return merged


def keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    t = (text or "").lower()
    hits = []
    for k in keywords:
        if k and k in t:
            hits.append(k)
    return sorted(set(hits))

