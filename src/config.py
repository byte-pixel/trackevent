from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    luma_home_url: str = "https://lu.ma/"
    judgment_labs_url: str = "https://www.judgmentlabs.ai/"
    days_ahead: int = 14

    # Very lightweight geo heuristic: we still let the agent use SF queries, but we
    # also filter events with these terms in venue/location text.
    sf_bay_terms: tuple[str, ...] = (
        "san francisco",
        "sf",
        "bay area",
        "oakland",
        "berkeley",
        "san jose",
        "palo alto",
        "mountain view",
        "redwood city",
        "menlo park",
        "santa clara",
        "sunnyvale",
        "fremont",
        "south san francisco",
    )

    out_dir: Path = Path("out")
    browser_profile_dir: Path = Path("out/browser_profile")


SETTINGS = Settings()

