"""Craigslist apartments: one small-radius search per enabled town."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from bs4 import BeautifulSoup

from .base import Fetcher, Log, SourceResult, assign_town, enabled_towns, parse_price

SEARCH_URL = "https://boston.craigslist.org/search/apa"
GONE_MARKERS = ("this posting has been deleted", "this posting has been flagged", "this posting has expired")


def parse_search(html: str) -> list[dict]:
    """Read the no-JavaScript result list plus the page's schema.org metadata."""
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, list[dict]] = defaultdict(list)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for element in (data.get("itemListElement") or []) if isinstance(data, dict) else []:
            item = element.get("item") or {}
            if item.get("name"):
                meta[item["name"]].append(item)

    results = []
    for li in soup.select("li.cl-static-search-result"):
        link = li.find("a", href=True)
        title_el = li.select_one(".title")
        if not link or not title_el:
            continue
        title = title_el.get_text(strip=True)
        url = link["href"]
        info = meta[title].pop(0) if meta.get(title) else {}
        address = info.get("address") or {}
        beds = info.get("numberOfBedrooms")
        price_el = li.select_one(".price")
        results.append({
            "source": "craigslist",
            "source_id": url.rstrip("/").rsplit("/", 1)[-1],
            "url": url,
            "title": title,
            "locality": address.get("addressLocality") or "",
            "state": address.get("addressRegion") or "",
            "lat": info.get("latitude"),
            "lng": info.get("longitude"),
            "baths": info.get("numberOfBathroomsTotal"),
            "units": [{
                "beds": float(beds) if beds is not None else None,
                "price_low": parse_price(price_el.get_text() if price_el else None),
                "price_high": None, "sqft": None,
            }],
        })
    return results


def fetch(settings: dict, fetcher: Fetcher, log: Log) -> SourceResult:
    cfg = settings["sources"]["craigslist"]
    criteria = settings["criteria"]
    params = {"search_distance": cfg.get("radius_miles", 4), "sort": "date"}
    if criteria.get("min_price"):
        params["min_price"] = criteria["min_price"]
    if criteria.get("max_price"):
        params["max_price"] = criteria["max_price"]
    if criteria.get("min_beds"):
        params["min_bedrooms"] = int(criteria["min_beds"])
    if criteria.get("max_beds") is not None:
        params["max_bedrooms"] = int(criteria["max_beds"])

    wanted = {t["name"] for t in enabled_towns(settings)}
    items: dict[str, dict] = {}
    result = SourceResult()
    for town in enabled_towns(settings):
        scope = f"town:{town['name']}"
        result.labels[scope] = town["name"]
        if not town.get("zip"):
            log(f"craigslist: {town['name']} has no ZIP code, skipping")
            result.skipped[scope] = "no ZIP code"
            continue
        try:
            resp = fetcher.get(SEARCH_URL, {**params, "postal": town["zip"]})
            resp.raise_for_status()
            found = parse_search(resp.text)
        except Exception as exc:  # one town failing should not sink the others
            result.failed[scope] = str(exc)[:200]
            log(f"craigslist: {town['name']} failed: {exc}")
            continue
        kept = 0
        for item in found:
            match = assign_town(settings["towns"], item.pop("locality"), item["lat"], item["lng"])
            if not match or match["name"] not in wanted:
                continue
            item["town"], item["state"] = match["name"], match.get("state", "MA")
            item["scope"] = scope
            if item["source_id"] not in items:
                kept += 1
            items.setdefault(item["source_id"], item)
        result.checked.add(scope)
        log(f"craigslist: {town['name']}: {len(found)} results, {kept} new in your towns")
    result.items = list(items.values())
    return result


def fetch_detail(url: str, fetcher: Fetcher) -> dict | None:
    """Fetch one posting. Returns None when the posting is gone."""
    resp = fetcher.get(url)
    if resp.status_code in (404, 410):
        return None
    resp.raise_for_status()
    html = resp.text
    if any(marker in html.lower() for marker in GONE_MARKERS):
        return None
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    body = soup.select_one("#postingbody")
    if body:
        for junk in body.select(".print-information, .print-qrcode-container"):
            junk.decompose()
        text = body.get_text("\n", strip=True).replace("QR Code Link to This Post", "").strip()
        out["description"] = re.sub(r"\n{3,}", "\n\n", text)

    posted = soup.select_one("time.date.timeago[datetime]")
    if posted:
        out["posted_at"] = posted["datetime"]

    map_el = soup.select_one("#map[data-latitude]")
    if map_el:
        try:
            out["lat"], out["lng"] = float(map_el["data-latitude"]), float(map_el["data-longitude"])
        except (KeyError, ValueError):
            pass

    street = soup.select_one("h2.street-address")
    if street:
        out["address"] = street.get_text(strip=True)

    images = re.search(r"var imgList = (\[.*?\]);", html, re.S)
    if images:
        try:
            out["images"] = [img["url"] for img in json.loads(images.group(1)) if img.get("url")][:12]
        except ValueError:
            pass

    amenities = []
    for attr in soup.select(".attrgroup .attr"):
        text = " ".join(attr.get_text(" ", strip=True).split())
        if not text:
            continue
        shape = re.match(r"(\d+)BR\s*/\s*([\d.]+)Ba", text, re.I)
        sqft = re.search(r"(\d{3,5})\s*ft", text)
        if shape:
            out["baths"] = float(shape.group(2))
        elif "available" in text.lower():
            out["available"] = text.lower().replace("available", "").strip() or "now"
        elif sqft and len(text) < 12:
            out["sqft"] = int(sqft.group(1))
        else:
            amenities.append(text)
    if amenities:
        out["amenities"] = amenities
    return out
