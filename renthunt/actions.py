"""Operations shared by the web API and the chat assistant."""
from __future__ import annotations

import statistics
import threading

import httpx

from . import ai, db, hunt

CRITERIA_TYPES = {
    "min_price": int, "max_price": int, "min_beds": float, "max_beds": float, "min_baths": float,
    "cats": bool, "dogs": bool, "must_have": list, "nice_to_have": list, "exclude": list,
    "max_age_days": int, "notes": str,
}


def _coerce(value, kind):
    if kind is bool:
        return value.strip().lower() in ("1", "true", "yes", "y") if isinstance(value, str) else bool(value)
    if kind is list:
        if isinstance(value, str):
            value = value.split(",")
        return [str(v).strip() for v in value if str(v).strip()]
    if kind in (int, float):
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
        return kind(float(value))
    return str(value)


def update_criteria(patch: dict) -> dict:
    """Apply a partial criteria update, re-score everything, return the new criteria."""
    settings = db.get_settings()
    criteria = settings["criteria"]
    unknown = [k for k in patch if k not in CRITERIA_TYPES]
    if unknown:
        raise ValueError(f"unknown criteria field(s): {', '.join(unknown)}. "
                         f"Valid fields: {', '.join(CRITERIA_TYPES)}")
    for key, value in patch.items():
        if value is None:
            continue
        try:
            criteria[key] = _coerce(value, CRITERIA_TYPES[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} has an invalid value: {value!r}") from None
    if criteria["min_price"] > criteria["max_price"]:
        raise ValueError("min_price is above max_price")
    if criteria["min_beds"] > criteria["max_beds"]:
        raise ValueError("min_beds is above max_beds")
    db.save_settings(settings)
    hunt.rescore_all()
    return criteria


def lookup_zip(zip_code: str) -> dict | None:
    """Town name and coordinates for a US ZIP code (public zippopotam.us lookup)."""
    try:
        resp = httpx.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=10)
        if resp.status_code != 200:
            return None
        place = resp.json()["places"][0]
        return {"name": place["place name"], "state": place["state abbreviation"],
                "lat": float(place["latitude"]), "lng": float(place["longitude"])}
    except Exception:
        return None


# Rough box around the Merrimack Valley, so "Chelmsford St" resolves here and not in another state.
GEOCODE_BOX = "-71.60,42.92,-70.90,42.42"


def geocode(place: str) -> dict | None:
    """Coordinates for a street, address or landmark (OpenStreetMap Nominatim; cached)."""
    key = "geo:" + " ".join(place.lower().split())
    cached = db.get_state(key)
    if cached:
        return cached
    try:
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "us", "addressdetails": 1,
                    "viewbox": GEOCODE_BOX, "bounded": 1},
            headers={"User-Agent": "RentHunt/0.1 (personal rental search tool)"}, timeout=12,
        )
        resp.raise_for_status()
        hits = resp.json()
    except Exception:
        return None
    if not hits:
        return None
    address = hits[0].get("address") or {}
    first = hits[0]["display_name"].split(",")[0].strip()
    if address.get("house_number") and address.get("road"):
        first = f"{address['house_number']} {address['road']}"
    town = address.get("city") or address.get("town") or address.get("village") or ""
    label = f"{first}, {town}" if town and town != first else first
    found = {"lat": float(hits[0]["lat"]), "lng": float(hits[0]["lon"]), "label": label}
    db.set_state(key, found)
    return found


def add_place(name: str, where: str) -> dict:
    """Save a person or place to measure listings against, e.g. ("Mom", "Andover, MA")."""
    name, where = (name or "").strip(), (where or "").strip()
    if not name or not where:
        raise ValueError("a name and a location (town or address) are both needed")
    spot = geocode(where)
    if not spot:
        raise ValueError(f"could not find '{where}' on the map; try adding the town or state")
    settings = db.get_settings()
    places = [p for p in settings["places"] if p["name"].lower() != name.lower()]
    place = {"name": name, "label": spot["label"], "lat": spot["lat"], "lng": spot["lng"]}
    settings["places"] = [*places, place]
    db.save_settings(settings)
    return place


def remove_place(name: str) -> None:
    settings = db.get_settings()
    settings["places"] = [p for p in settings["places"] if p["name"].lower() != name.strip().lower()]
    db.save_settings(settings)


def set_towns(enable: list[str] = (), disable: list[str] = (), add: list[dict] = ()) -> dict:
    settings = db.get_settings()
    towns = settings["towns"]
    by_name = {t["name"].lower(): t for t in towns}
    problems = []
    for entry in add or []:
        name = str(entry.get("name") or "").strip()
        zip_code = str(entry.get("zip") or "").strip()
        if name.lower() in by_name:
            by_name[name.lower()]["enabled"] = True
            continue
        if not (zip_code.isdigit() and len(zip_code) == 5):
            problems.append(f"{name or 'new town'}: a 5-digit ZIP code is needed to add a town")
            continue
        found = lookup_zip(zip_code) or {}
        town = {"name": name or found.get("name") or zip_code,
                "state": str(entry.get("state") or found.get("state") or "MA").upper(),
                "zip": zip_code, "lat": found.get("lat"), "lng": found.get("lng"), "enabled": True}
        towns.append(town)
        by_name[town["name"].lower()] = town
    for names, flag in ((enable, True), (disable, False)):
        for name in names or []:
            town = by_name.get(str(name).strip().lower())
            if town:
                town["enabled"] = flag
            else:
                problems.append(f"{name}: not in the town list (add it with its ZIP code)")
    db.save_settings(settings)
    hunt.rescore_all()
    return {"enabled": [t["name"] for t in towns if t["enabled"]],
            "available": [t["name"] for t in towns if not t["enabled"]],
            "problems": problems}


def update_schedule(patch: dict) -> dict:
    settings = db.get_settings()
    sched = settings["schedule"]
    if patch.get("enabled") is not None:
        sched["enabled"] = _coerce(patch["enabled"], bool)
    if patch.get("interval_hours") is not None:
        sched["interval_hours"] = min(168.0, max(0.5, float(patch["interval_hours"])))
    for key in ("quiet_start", "quiet_end"):
        if patch.get(key) is not None:
            sched[key] = int(patch[key]) % 24
    db.save_settings(settings)
    return sched


def add_watch(url: str, label: str = "") -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("the address must start with http:// or https://")
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO watch_urls (url, label) VALUES (?, ?)", (url, label.strip()))
        row = conn.execute("SELECT * FROM watch_urls WHERE url=?", (url,)).fetchone()
    return dict(row)


def list_watches() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM watch_urls ORDER BY id")]


def start_hunt(trigger: str = "manual") -> bool:
    """Kick off a hunt in the background. False if one is already running."""
    if hunt.is_running():
        return False
    threading.Thread(target=hunt.run, args=(trigger,), daemon=True, name="hunt").start()
    return True


def market_stats() -> list[dict]:
    """Median advertised rent by town and bedroom count, over active listings."""
    listings, _ = db.query_listings(view="all", limit=5000)
    groups: dict = {}
    for listing in listings:
        if not listing["active"] or not listing["town"]:
            continue
        for unit in listing["units"]:
            if unit.get("price_low") and unit.get("beds") is not None:
                groups.setdefault((listing["town"], unit["beds"]), []).append(unit["price_low"])
    return [
        {"town": town, "beds": beds, "listings": len(prices),
         "median_rent": int(statistics.median(prices)), "lowest_rent": min(prices)}
        for (town, beds), prices in sorted(groups.items()) if len(prices) >= 2
    ]


def overview() -> dict:
    settings = db.get_settings()
    last = hunt.last_run()
    due = hunt.next_run_time(settings)
    return {
        "counts": db.counts(),
        "towns": [t["name"] for t in settings["towns"] if t.get("enabled")],
        "places": settings["places"],
        "running": hunt.is_running(),
        "step": hunt.progress["step"],
        "last_run": last and {k: last[k] for k in ("id", "started_at", "finished_at", "status", "trigger", "summary")},
        "next_run": due.isoformat(timespec="seconds") if due else None,
        "ai": ai.status(settings),
    }
