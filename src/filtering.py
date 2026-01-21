from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from dateutil import parser as date_parser

from .judgment_topics import keyword_hits
from .models import Event


def parse_datetime_loose(s: str) -> datetime | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    try:
        dt = date_parser.parse(s, fuzzy=True)
    except Exception:
        return None
    # If no tzinfo, assume local-ish; keep naive.
    return dt


def is_within_days(start_at: datetime | None, *, days: int, now: datetime | None = None) -> bool:
    if not start_at:
        return False
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    end = now + timedelta(days=days)
    return now <= start_at <= end


def looks_like_sf_bay(text: str, sf_terms: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    if "online" in t or "virtual" in t:
        return True  # allow online even if no geo string
    return any(term in t for term in sf_terms)


def score_relevance(event: Event, keywords: set[str]) -> Event:
    blob = " ".join(
        [
            event.title or "",
            event.description or "",
            " ".join(event.tags or []),
            event.venue.raw or "",
            event.organizer.name or "",
        ]
    )
    hits = keyword_hits(blob, keywords)
    print(f"[DEBUG] keyword hits: {hits}")
    # Simple scoring: title hits weigh more.
    title_hits = keyword_hits(event.title or "", keywords)
    score = float(len(hits) + len(title_hits) * 1.5)
    event.relevance_score = score
    event.matched_keywords = hits
    return event

