"""Shared plumbing for listing sources.

Every source returns a list of plain dicts ("items") with these keys (all but the
first four optional): source, source_id, url, title, town, state, address, lat, lng,
units [{beds, price_low, price_high, sqft}], baths, sqft, description, images,
amenities, contact, posted_at, available.
"""
from __future__ import annotations

import math
import re
import time
from typing import Callable

import httpx

from .. import config

Log = Callable[[str], None]


class Fetcher:
    """HTTP client that spaces requests out so we stay a light, polite visitor."""

    def __init__(self, delay: float, log: Log):
        self.delay = max(0.5, float(delay))
        self.log = log
        self._last = 0.0
        self.client = httpx.Client(
            headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True, timeout=30,
        )

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            return self.client.get(url, params=params)
        finally:
            self._last = time.monotonic()

    def close(self) -> None:
        self.client.close()


def miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def assign_town(towns: list[dict], locality: str | None, lat=None, lng=None) -> dict | None:
    """Map a free-text locality (or coordinates) onto one of the configured towns."""
    loc = (locality or "").lower()
    if loc:
        # Longest name first so "North Andover" is not swallowed by "Andover".
        for town in sorted(towns, key=lambda t: -len(t["name"])):
            if re.search(rf"\b{re.escape(town['name'].lower())}\b", loc):
                return town
    if lat is not None and lng is not None:
        located = [t for t in towns if t.get("lat") is not None and t.get("lng") is not None]
        if located:
            nearest = min(located, key=lambda t: miles(lat, lng, t["lat"], t["lng"]))
            if miles(lat, lng, nearest["lat"], nearest["lng"]) <= 2.5:
                return nearest
    return None


def enabled_towns(settings: dict) -> list[dict]:
    return [t for t in settings["towns"] if t.get("enabled")]


def parse_price(text: str | None) -> int | None:
    match = re.search(r"\$?\s*(\d[\d,]{2,})", text or "")
    return int(match.group(1).replace(",", "")) if match else None
