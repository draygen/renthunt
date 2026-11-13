"""Listings a page states in a machine-readable form, read without the model.

Three readers, tried in order by `extract()`:

- AppFolio listing pages (`<company>.appfolio.com/listings`), the vacancy list many local
  property managers link to. The markup is the same for every company.
- RentPress search pages: apartment companies whose WordPress site uses the RentPress
  plugin (Princeton Properties, for one) put every community, with its rent per bedroom
  count and what is open, in a `data-properties` attribute. One listing per community.
- schema.org JSON-LD (`<script type="application/ld+json">`) describing apartments,
  houses or offers with a rent.

Each reader returns "raw" listings: dicts with the same keys the model produces
(title, url, town, address, price, beds, baths, sqft, available, description, contact)
plus optional lat, lng, images and amenities. A community with several floor plans gives
`units` ([{beds, price_low, price_high, sqft}]) instead of one beds/price pair. Only
entries with a rent are returned.
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

RENTAL_TYPES = {
    "apartment", "apartmentcomplex", "house", "singlefamilyresidence", "residence",
    "accommodation", "suite", "room", "realestatelisting", "product", "offer", "floorplan",
    "gatedresidencecommunity",
}


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):  # QuantitativeValue
        return _number(value.get("value") if value.get("value") is not None else value.get("minValue"))
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


def _whole(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _text(soup_node) -> str:
    return " ".join(soup_node.get_text(" ", strip=True).split()) if soup_node else ""


# ---------- AppFolio ----------

def appfolio(html: str, base_url: str) -> list[dict] | None:
    """Listings from an AppFolio page, or None when the page is not one.

    An empty list is a real answer: the page is an AppFolio listing page showing no units.
    """
    if "js-listing-item" not in html and "js-listings-container" not in html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one(".js-listings-container, #result_container, .js-listing-item"):
        return None
    spots = {}
    markers = re.search(r"markers:\s*(\[.*?\])\s*,\s*\n", html, re.S)
    if markers:
        try:
            spots = {m.get("listing_id"): m for m in json.loads(markers.group(1))}
        except ValueError:
            spots = {}
    out = []
    for node in soup.select(".js-listing-item"):
        address = _text(node.select_one(".js-listing-address"))
        price = _whole(_text(node.select_one(".js-listing-blurb-rent")))
        if not address or not price:
            continue
        shape = _text(node.select_one(".js-listing-blurb-bed-bath")).lower()
        beds = re.search(r"([\d.]+)\s*bd", shape)
        baths = re.search(r"([\d.]+)\s*ba", shape)
        link = node.select_one(".js-listing-title a[href], a.js-link-to-detail[href], a[href*='/listings/detail/']")
        image = node.select_one("img[data-original]")
        details = [_text(p) for p in node.select(".js-listing-description, .listing-item__body p.hand-hidden")]
        listing_id = re.sub(r"\D", "", node.get("id") or "")
        spot = spots.get(int(listing_id)) if listing_id else None
        parts = [p.strip() for p in address.split(",")]
        out.append({
            "title": address.rsplit(",", 2)[0] if address.count(",") >= 2 else address,
            "headline": _text(node.select_one(".js-listing-title")),
            "url": urljoin(base_url, link["href"]) if link else base_url,
            "address": address,
            "town": parts[-2] if len(parts) >= 3 else None,
            "price": price,
            "beds": 0.0 if "studio" in shape else float(beds.group(1)) if beds else None,
            "baths": float(baths.group(1)) if baths else None,
            "sqft": _whole(_text(node.select_one(".js-listing-square-feet"))),
            "available": _text(node.select_one(".js-listing-available")).removeprefix("Available").strip() or None,
            "description": "\n".join(dict.fromkeys(d for d in details if d)),
            "lat": (spot or {}).get("latitude"), "lng": (spot or {}).get("longitude"),
            "images": [image["data-original"]] if image else [],
        })
    return out


# ---------- RentPress ----------

DATA_PROPERTIES = re.compile(r"""data-properties\s*=\s*(["'])(.*?)\1""", re.S)


def _listed(value) -> list:
    """RentPress stores lists and tables as JSON text inside the JSON."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def data_properties(html: str) -> str | None:
    """The raw RentPress attribute, also used to notice when the page's rents change."""
    found = DATA_PROPERTIES.search(html) if "rentpress" in html and "data-properties" in html else None
    return found.group(2) if found else None


def rentpress(html: str, base_url: str) -> list[dict] | None:
    """Communities with something open, from a RentPress page; None when it is not one."""
    blob = data_properties(html)
    if blob is None:
        return None
    try:
        properties = json.loads(unescape(blob))
    except ValueError:
        return None
    if not isinstance(properties, list):
        return None
    out = []
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        matrix = prop.get("property_availability_matrix")
        if isinstance(matrix, str):
            try:
                matrix = json.loads(matrix)
            except ValueError:
                matrix = None
        units = []
        for key, plan in (matrix.items() if isinstance(matrix, dict) else []):
            beds = re.match(r"(\d+)bed$", str(key))
            price = _whole(plan.get("price")) if isinstance(plan, dict) else None
            if beds and price and price >= 200 and plan.get("available"):
                units.append({"beds": float(beds.group(1)), "price_low": price, "price_high": None, "sqft": None})
        name = str(prop.get("property_name") or "").strip()
        if not units or not name:
            continue
        town = str(prop.get("property_city") or "").strip()
        tail = " ".join(str(p) for p in (prop.get("property_state"), prop.get("property_zip")) if p)
        address = ", ".join(p for p in (str(prop.get("property_address") or "").strip(), town, tail) if p)
        open_units = _whole(prop.get("property_available_units"))
        pets = _listed(prop.get("property_pets"))
        about = BeautifulSoup(str(prop.get("property_description") or ""), "html.parser").get_text(" ", strip=True)
        special = str(prop.get("property_specials_message") or "").strip()
        description = "\n".join(p for p in (
            about[:1500], f"Special: {special}" if special else "",
            "Pets: " + ", ".join(map(str, pets)) if pets else "") if p)
        link = prop.get("property_post_link")
        image = prop.get("property_featured_image_src")
        out.append({
            "title": name.title() if name.isupper() else name,
            "url": urljoin(base_url, link) if isinstance(link, str) and link else base_url,
            "address": address or None, "town": town or None,
            "units": sorted(units, key=lambda u: u["beds"]),
            "available": f"{open_units} unit{'s' if open_units != 1 else ''} available" if open_units else None,
            "description": description,
            "amenities": [str(a) for a in _listed(prop.get("property_community_amenities"))][:40],
            "contact": prop.get("property_phone_number") or None,
            "lat": _number(prop.get("property_latitude")), "lng": _number(prop.get("property_longitude")),
            "images": [image] if isinstance(image, str) and image else [],
        })
    return out


# ---------- schema.org JSON-LD ----------

def _nodes(data):
    """Every dict inside a JSON-LD document, at any depth."""
    if isinstance(data, list):
        for entry in data:
            yield from _nodes(entry)
    elif isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _nodes(value)


def _types(node: dict) -> set[str]:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return {str(k).rsplit("/", 1)[-1].lower() for k in kinds if k}


def _offer_price(node: dict) -> int | None:
    offers = node.get("offers")
    for offer in offers if isinstance(offers, list) else [offers]:
        if not isinstance(offer, dict):
            continue
        spec = offer.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        for value in (offer.get("price"), offer.get("lowPrice"),
                      spec.get("price") if isinstance(spec, dict) else None,
                      spec.get("minPrice") if isinstance(spec, dict) else None):
            price = _whole(value)
            if price and price >= 200:
                return price
    price = _whole(node.get("price"))
    return price if price and price >= 200 else None


def _address(value) -> tuple[str | None, str | None]:
    """(full address, town) from a schema.org address in either form."""
    if isinstance(value, str):
        return value.strip() or None, None
    if not isinstance(value, dict):
        return None, None
    town = value.get("addressLocality")
    tail = " ".join(str(p) for p in (value.get("addressRegion"), value.get("postalCode")) if p)
    full = ", ".join(str(p).strip() for p in (value.get("streetAddress"), town, tail) if p)
    return full or None, town


def json_ld(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for node in _nodes(data):
            if not (_types(node) & RENTAL_TYPES):
                continue
            price = _offer_price(node)
            home = node
            if "offer" in _types(node) and isinstance(node.get("itemOffered"), dict):
                home = {**node["itemOffered"], "offers": node}
            elif isinstance(node.get("mainEntity"), dict):  # RealEstateListing wraps the home
                home = {**node["mainEntity"], **{k: v for k, v in node.items() if k in ("offers", "url")}}
            price = price or _offer_price(home)
            address, town = _address(home.get("address"))
            name = str(home.get("name") or "").strip()
            if not price or not (address or name):
                continue
            beds = home.get("numberOfBedrooms")
            offers = home.get("offers") if isinstance(home.get("offers"), dict) else {}
            geo = home.get("geo") if isinstance(home.get("geo"), dict) else {}
            image = home.get("image")
            images = [image] if isinstance(image, str) else [
                i if isinstance(i, str) else i.get("url") for i in image or [] if isinstance(i, (str, dict))
            ] if isinstance(image, list) else []
            url = home.get("url") if isinstance(home.get("url"), str) else None
            key = (address or name, _number(beds), price)
            if key in seen:  # the same home is often described by nested nodes
                continue
            seen.add(key)
            out.append({
                "title": name or address,
                "url": urljoin(base_url, url) if url else base_url,
                "address": address, "town": town,
                "price": price,
                "beds": _number(beds),
                "baths": _number(home.get("numberOfBathroomsTotal") or home.get("numberOfFullBathrooms")),
                "sqft": _whole(home.get("floorSize")),
                "available": home.get("availabilityStarts") or offers.get("availabilityStarts"),
                "description": str(home.get("description") or "")[:2000],
                "contact": home.get("telephone") if isinstance(home.get("telephone"), str) else None,
                "lat": _number(geo.get("latitude")), "lng": _number(geo.get("longitude")),
                "images": [i for i in images if isinstance(i, str)][:8],
            })
    return out


def extract(html: str, base_url: str) -> tuple[list[dict], str] | None:
    """(raw listings, how they were read), or None when the page has nothing structured.

    An AppFolio or RentPress page with nothing open comes back as ([], ...): a confirmed
    empty list.
    JSON-LD without a single rented home tells us nothing, so that is None.
    """
    found = appfolio(html, base_url)
    if found is not None:
        return found, "AppFolio listing page"
    found = rentpress(html, base_url)
    if found is not None:
        return found, "RentPress community data"
    found = json_ld(html, base_url)
    if found:
        return found, "structured data"
    return None
