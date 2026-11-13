"""Rent.com apartment communities, read from the page's embedded search data."""
from __future__ import annotations

import json
import re

from .base import Fetcher, Log, SourceResult, enabled_towns

STATE_SLUGS = {"MA": "massachusetts", "NH": "new-hampshire"}
IMAGE_URL = "https://i.rent.com/t_3x2_fixed_webp_md/{id}"


def parse_search(html: str) -> list[dict]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("page layout changed: no embedded search data")
    try:
        search = json.loads(match.group(1))["props"]["pageProps"]["pageData"]["location"]["listingSearch"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"page layout changed: {exc!r}") from exc

    streets = {}
    for block in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("url") and isinstance(data.get("address"), dict):
            streets[data["url"]] = data["address"].get("streetAddress")

    availability = {r.get("listingId"): r for r in search.get("filterMatchResults") or []}
    items = []
    for listing in search.get("listings") or []:
        if listing.get("offMarket") or not listing.get("id") or not listing.get("urlPathname"):
            continue
        extra = availability.get(listing["id"], {})
        units = []
        for bed in extra.get("bedCountData") or listing.get("bedCountData") or []:
            prices = bed.get("prices") or {}
            if prices.get("low") is None:
                continue
            units.append({
                "beds": float(bed.get("beds", 0)),
                "price_low": int(prices["low"]),
                "price_high": int(prices["high"]) if prices.get("high") else None,
                "sqft": (bed.get("squareFeet") or {}).get("low"),
            })
        if not units:
            continue
        loc = listing.get("location") or {}
        url = "https://www.rent.com" + listing["urlPathname"]
        street = streets.get(url)
        city, state, zip_code = loc.get("city") or "", loc.get("stateAbbr") or "", loc.get("zip") or ""
        manager = (listing.get("propertyManagementCompany") or {}).get("name")
        available = extra.get("unitsAvailableText") or (
            "now" if listing.get("availabilityStatus") == "TODAY" else None)
        items.append({
            "source": "rentcom",
            "source_id": listing["id"],
            "url": url,
            "title": listing.get("name") or "Apartment community",
            "locality": city,
            "state": state,
            "address": ", ".join(p for p in (street, city, f"{state} {zip_code}".strip()) if p),
            "lat": loc.get("lat"), "lng": loc.get("lng"),
            "units": units,
            "baths": (extra.get("baths") or {}).get("low"),
            "sqft": (extra.get("sqFtRange") or {}).get("min"),
            "description": f"Managed by {manager}." if manager else "",
            "images": [IMAGE_URL.format(id=p["id"]) for p in (listing.get("photos") or [])[:8] if p.get("id")],
            "amenities": list(listing.get("amenitiesHighlighted") or []),
            "contact": listing.get("phoneDesktopText"),
            "available": available,
        })
    return items


def fetch(settings: dict, fetcher: Fetcher, log: Log) -> SourceResult:
    pages = max(1, int(settings["sources"]["rentcom"].get("pages_per_town", 1)))
    wanted = {t["name"].lower(): t for t in enabled_towns(settings)}
    items: dict[str, dict] = {}
    result = SourceResult()
    for town in enabled_towns(settings):
        scope = f"town:{town['name']}"
        result.labels[scope] = town["name"]
        state_slug = STATE_SLUGS.get(town.get("state", "MA"))
        if not state_slug:
            log(f"rent.com: no state mapping for {town['name']}, skipping")
            result.skipped[scope] = "no state mapping"
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", town["name"].lower()).strip("-")
        for page in range(1, pages + 1):
            url = f"https://www.rent.com/{state_slug}/{slug}-apartments"
            try:
                resp = fetcher.get(url, {"page": page} if page > 1 else None)
                resp.raise_for_status()
                found = parse_search(resp.text)
            except Exception as exc:
                # Results from earlier pages are kept, but the town was not read in full.
                result.failed[scope] = f"page {page}: {str(exc)[:200]}"
                log(f"rent.com: {town['name']} page {page} failed: {exc}")
                break
            kept = 0
            for item in found:
                match = wanted.get(item.pop("locality").lower())
                if not match:
                    continue  # result pages pad out with neighbouring towns
                item["town"] = match["name"]
                item["scope"] = scope
                if item["source_id"] not in items:
                    kept += 1
                items.setdefault(item["source_id"], item)
            log(f"rent.com: {town['name']} page {page}: {len(found)} results, {kept} new in your towns")
            if kept == 0:
                break
        if scope not in result.failed:
            result.checked.add(scope)
    result.items = list(items.values())
    return result
