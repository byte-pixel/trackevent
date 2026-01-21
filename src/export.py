from __future__ import annotations

from pathlib import Path

import pandas as pd

from .models import Event


def ensure_out_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)


def export_events(events: list[Event], out_dir: Path) -> tuple[Path, Path]:
    ensure_out_dir(out_dir)
    json_path = out_dir / "events.json"
    csv_path = out_dir / "events.csv"

    # Use JSON Lines for easier streaming/inspection.
    json_path.write_text("\n".join(e.model_dump_json() for e in events) + ("\n" if events else ""), encoding="utf-8")

    rows: list[dict] = []
    for e in events:
        d = e.model_dump(mode="json")
        d["venue_raw"] = d.get("venue", {}).get("raw")
        d["venue_is_online"] = d.get("venue", {}).get("is_online")
        d["organizer_name"] = d.get("organizer", {}).get("name")
        d.pop("venue", None)
        d.pop("organizer", None)
        rows.append(d)

    pd.DataFrame(rows).to_csv(csv_path, index=False)
    return json_path, csv_path

