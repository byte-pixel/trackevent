from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class Organizer(BaseModel):
    name: Optional[str] = None


class Venue(BaseModel):
    raw: Optional[str] = None
    is_online: bool = False


class Event(BaseModel):
    url: HttpUrl
    title: str
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    timezone: Optional[str] = None
    venue: Venue = Field(default_factory=Venue)
    organizer: Organizer = Field(default_factory=Organizer)
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)

    relevance_score: float = 0.0
    matched_keywords: list[str] = Field(default_factory=list)
    relevance_reason: Optional[str] = None  # Why this event was included

