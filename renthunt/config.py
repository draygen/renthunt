"""Paths, environment loading and default settings."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines into the environment. Values already set in the environment win."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env_file(ROOT / ".env")  # secrets such as RENTCAST_API_KEY; the file is git-ignored
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
        "cat_count": 1,  # how many cats; 2+ also watches for one-cat limits
        "move_in": "any",  # "asap" favours places available now and drops ones far in the future
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
        "watch": {"enabled": True, "render_timeout_seconds": 30},  # pages you add (see sources/ai_sources.py)
        # Optional paid API, off by default. The key is read from RENTCAST_API_KEY, never from here.
        "rentcast": {
            "enabled": False,
            "max_requests_per_hunt": 8,     # one request per town, more when a town has over 500 results
            "max_requests_per_month": 45,   # hard stop; RentCast's free plan allows 50
            "hours_between_checks": 168,    # do not ask about the same town more often than this
            "max_pages_per_town": 2,
            "billing_day": 1,               # day of the month your RentCast plan renews
        },
        # Listing alerts emailed by sites that block automated visits (see sources/mail.py).
        # The mailbox sign-in is read from RENTHUNT_MAIL_USER / RENTHUNT_MAIL_PASSWORD, never from here.
        "mail": {
            "enabled": False,
            "gmail_token_file": "",       # a Google read-only OAuth token file; empty = use IMAP below
            "host": "imap.gmail.com", "port": 993, "folder": "INBOX",
            "senders": ["zillow.com", "apartments.com", "hotpads.com", "realtor.com", "trulia.com",
                        "redfin.com", "zumper.com", "padmapper.com"],
            "days_back": 7,               # how far back to look for alerts not read yet
            "max_messages_per_hunt": 15,  # each email is one or more local-model calls
            "keep_days": 21,              # drop an emailed listing no alert has mentioned for this long
        },
    },
    "schedule": {
        "enabled": True,
        "interval_hours": 4,
        # Between full hunts, re-check just the sources that are cheap to read, so a new post
        # is seen within minutes. 0 switches the quick checks off.
        "quick_minutes": 30,
        "quick_sources": ["craigslist", "watch", "mail"],
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
