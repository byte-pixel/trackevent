from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from src.config import SETTINGS
from src.export import export_events
from src.judgment_topics import build_judgment_keyword_set
from src.luma_scrape_agent import scrape_luma_events_with_agent


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=SETTINGS.days_ahead)
    parser.add_argument("--region", type=str, default="sf_bay", choices=["sf_bay"])
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument("--max-events", type=int, default=50)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Missing env var ANTHROPIC_API_KEY")

    out_dir: Path = SETTINGS.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    keywords = build_judgment_keyword_set(SETTINGS.judgment_labs_url)

    events = scrape_luma_events_with_agent(
        luma_home_url=SETTINGS.luma_home_url,
        days=args.days,
        region=args.region,
        sf_terms=SETTINGS.sf_bay_terms,
        keywords=keywords,
        out_dir=out_dir,
        browser_profile_dir=SETTINGS.browser_profile_dir,
        headless=args.headless,
        max_events=args.max_events,
    )

    json_path, csv_path = export_events(events, out_dir=out_dir)
    print(f"Wrote {len(events)} events to: {json_path}")
    print(f"Wrote {len(events)} events to: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

