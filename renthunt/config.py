"""Paths, environment loading and default settings."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RENTHUNT_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "renthunt.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)


def _town(name: str, zip_code: str, lat: float, lng: float, enabled: bool) -> dict:
    return {"name": name, "state": "MA", "zip": zip_code, "lat": lat, "lng": lng, "enabled": enabled}


DEFAULT_SETTINGS: dict = {
    # Towns are searched in list order; earlier towns score slightly higher.
    "towns": [
        _town("Lowell", "01852", 42.6334, -71.3162, True),
        _town("Dracut", "01826", 42.6704, -71.3020, True),
        _town("Chelmsford", "01824", 42.5998, -71.3673, True),
        _town("Lawrence", "01841", 42.7070, -71.1631, True),
        _town("Haverhill", "01830", 42.7762, -71.0773, True),
        _town("Methuen", "01844", 42.7262, -71.1909, True),
        _town("Tewksbury", "01876", 42.6106, -71.2342, True),
        _town("Billerica", "01821", 42.5584, -71.2689, True),
        _town("Andover", "01810", 42.6583, -71.1368, False),
        _town("North Andover", "01845", 42.6987, -71.1351, False),
        _town("Tyngsborough", "01879", 42.6767, -71.4245, False),
        _town("Westford", "01886", 42.5793, -71.4378, False),
        _town("Wilmington", "01887", 42.5465, -71.1737, False),
    ],
    "criteria": {
        "min_price": 500,
        "max_price": 2500,
        "min_beds": 1,
        "max_beds": 3,
        "min_baths": 0,
        "cats": False,  # require cats allowed
        "dogs": False,  # require dogs allowed
        "must_have": [],  # every phrase must appear in the listing text
        "nice_to_have": ["parking", "laundry", "heat included", "hot water included"],
        "exclude": ["roommate", "room for rent", "private room", "shared room", "sublet", "55+", "senior living"],
        "max_age_days": 30,  # ignore posts older than this (when a post date is known)
        "notes": "",  # free-text preferences, shown to the chat assistant
    },
    "sources": {
        "craigslist": {"enabled": True, "radius_miles": 4, "detail_fetch_limit": 25},
        "rentcom": {"enabled": True, "pages_per_town": 1},
        "watch": {"enabled": True},  # pages you add, read by the local model
    },
    "schedule": {
        "enabled": True,
        "interval_hours": 4,
        "quiet_start": 23,  # no runs from 23:00 ...
        "quiet_end": 7,  # ... until 07:00 local time
    },
    "notify": {
        "min_score": 60,
        "webhook_url": "",  # ntfy.sh topic URL or Discord/Slack webhook; empty = in-app only
    },
    "ai": {
        "base_url": "http://127.0.0.1:11434",  # Ollama
        "model": "qwen3:8b",
        "num_ctx": 16384,  # context window to request; larger uses more VRAM
        "think": False,  # let the model reason before answering (slower, a bit smarter)
    },
    # People and places you want to live near: [{"name", "label", "lat", "lng"}]. Each one gets a
    # distance badge on every listing and a marker on the map. Shown only to the signed-in owner.
    "places": [],
    "request_delay_seconds": 2.5,
    "inactive_after_missed_runs": 3,
}
