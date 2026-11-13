"""Spot the same home listed more than once, within a source or across sources.

Two listings are treated as one home only when their street address (house number and
street), town and state agree, and then:

- both give a unit and the units are the same; or
- at least one gives no unit, and bedrooms, rent (within 5%), and any bathrooms and
  square footage both state all agree.

Listings that give different units are never merged, and neither are a whole community
(several floor plans) and a single unit inside it. A listing with no unit that could
belong to two different known units is left alone rather than guessed at. Title and rent
alone never merge listings from different sources; the one exception is the long-standing
rule for Craigslist reposts with no usable address (same title, town and rent).

Nothing is deleted: every source keeps its own row. The copies just point at one of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SUFFIXES = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "boulevard": "blvd", "lane": "ln", "court": "ct", "place": "pl", "terrace": "ter", "terr": "ter",
    "highway": "hwy", "parkway": "pkwy", "circle": "cir", "square": "sq", "route": "rt", "rte": "rt",
    "extension": "ext", "trail": "trl", "heights": "hts", "north": "n", "south": "s", "east": "e",
    "west": "w", "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}
STREET_ENDS = set(SUFFIXES.values()) - {"n", "s", "e", "w", "ne", "nw", "se", "sw"} | {"way", "row", "path", "run"}
UNIT = re.compile(r"(?:\b(?:apt|apartment|unit|ste|suite|rm|room)\b\.?\s*#?\s*|#\s*)([a-z0-9][a-z0-9-]*)")
FLOOR = re.compile(r"\b(\d+)(?:st|nd|rd|th)?\s+(?:fl|flr|floor)\b|\b(?:fl|flr|floor)\.?\s*(\d+)\b")
STREET = re.compile(r"^(\d+[a-z]?(?:\s*-\s*\d+[a-z]?)?)\s+([a-z0-9 ]+)$")
ENGAGED_STATUSES = ("contacted", "touring", "applied", "rejected")


@dataclass(frozen=True)
class Home:
    number: str
    street: str
    town: str
    state: str
    unit: str  # "" when the listing gives none

    @property
    def building(self) -> tuple[str, str, str]:
        return (self.number, self.street, self.town)


def _split(text: str, town: str) -> tuple[str, str] | None:
    """(house number, normalized street) and the unit, from one address-like string."""
    text = text.lower().replace(".", " ")
    text = re.split(r"\s+(?:near|at|and|&|@|between)\s+", text)[0]
    unit = ""
    found = UNIT.search(text)
    if found:
        unit = found.group(1)
        text = text[:found.start()] + " " + text[found.end():]
    else:
        floor = FLOOR.search(text)
        if floor:
            unit = "fl" + (floor.group(1) or floor.group(2))
            text = text[:floor.start()] + " " + text[floor.end():]
    street = re.sub(r"[^a-z0-9 -]", " ", text.split(",")[0])
    street = re.sub(r"\b[a-z]{2}\s+\d{5}(-\d{4})?\s*$", " ", street)  # "ma 01852" with no comma before it
    if town and street.rstrip().endswith(" " + town):
        street = street.rstrip()[: -len(town)]
    match = STREET.match(" ".join(street.split()).strip(" -"))
    if not match:
        return None
    words = [SUFFIXES.get(word, word) for word in match.group(2).split()]
    number = re.sub(r"\s", "", match.group(1))
    unit = unit.replace("-", "").lstrip("0")  # "000" is a placeholder, not a unit
    return f"{number}|{' '.join(words)}", unit


def home_of(listing: dict) -> Home | None:
    """The address this listing claims, or None when it does not give a street number."""
    town = (listing.get("town") or "").strip().lower()
    if not town:
        return None
    found = _split(listing.get("address") or "", town)
    if not found:
        # Some sources put the street address in the title ("140 Bailey St").
        title = (listing.get("title") or "").split(",")[0]
        guess = _split(title, town)
        if guess and len(title.split()) <= 7 and guess[0].split()[-1] in STREET_ENDS:
            found = guess
    if not found:
        return None
    number, street = found[0].split("|")
    return Home(number, street, town, (listing.get("state") or "").strip().upper(), found[1])


def _is_community(listing: dict) -> bool:
    return len(listing.get("units") or []) > 1


def _close(a, b, tolerance: float) -> bool:
    return a is None or b is None or abs(a - b) <= tolerance * max(abs(a), abs(b))


def same_home(a: dict, home_a: Home, b: dict, home_b: Home) -> bool:
    if home_a.building != home_b.building:
        return False
    if home_a.state and home_b.state and home_a.state != home_b.state:
        return False
    if home_a.unit and home_b.unit and home_a.unit != home_b.unit:
        return False
    if _is_community(a) != _is_community(b):
        return False
    if _is_community(a):
        return not (home_a.unit or home_b.unit) or home_a.unit == home_b.unit
    beds_a, beds_b = a.get("beds_min"), b.get("beds_min")
    if home_a.unit and home_a.unit == home_b.unit:
        return beds_a is None or beds_b is None or beds_a == beds_b
    # No unit on at least one side: the supporting details have to carry it.
    if beds_a is None or beds_b is None or beds_a != beds_b:
        return False
    if a.get("price") is None or b.get("price") is None or not _close(a["price"], b["price"], 0.05):
        return False
    if a["source"] == b["source"] and a["price"] != b["price"]:
        return False  # one source listing two rents at an address is listing two homes
    if a.get("baths") is not None and b.get("baths") is not None and a["baths"] != b["baths"]:
        return False
    return _close(a.get("sqft") or None, b.get("sqft") or None, 0.10)


def is_engaged(listing: dict) -> bool:
    """Has the owner done something with this listing that must not be lost?"""
    return bool(listing.get("favorite") or listing.get("hidden") or (listing.get("notes") or "").strip()
                or listing.get("status") in ENGAGED_STATUSES)


def _legacy_key(listing: dict) -> str | None:
    if listing["source"] != "craigslist":
        return None
    title = re.sub(r"[^a-z0-9]+", " ", listing["title"].lower()).strip()
    return f"{title}|{listing.get('town')}|{listing.get('price')}"


def group(listings: list[dict]) -> dict[int, dict]:
    """For each listing id: {"key": str | None, "dup_of": int | None, "group": [ids]}.

    `listings` should be every stored listing. Only active ones are grouped. The listing
    the others point at is the one the owner has worked with if there is one, otherwise
    the first one seen.
    """
    out = {l["id"]: {"key": None, "dup_of": None, "group": [l["id"]]} for l in listings}
    ranked = sorted((l for l in listings if l.get("active")), key=lambda l: (not is_engaged(l), l["id"]))
    homes = {l["id"]: home_of(l) for l in ranked}
    by_id = {l["id"]: l for l in ranked}

    buildings: dict[tuple, list[list[int]]] = {}
    legacy: dict[str, int] = {}
    for listing in ranked:
        home = homes[listing["id"]]
        if home is None:
            key = _legacy_key(listing)
            out[listing["id"]]["key"] = key
            if key:
                first = legacy.setdefault(key, listing["id"])
                if first != listing["id"]:
                    out[listing["id"]]["dup_of"] = first
            continue
        out[listing["id"]]["key"] = "addr:" + "|".join([*home.building, home.state, home.unit])
        clusters = buildings.setdefault(home.building, [])
        fits = [c for c in clusters
                if all(same_home(listing, home, by_id[i], homes[i]) for i in c)]
        if len(fits) == 1:
            fits[0].append(listing["id"])
        else:  # nothing matches, or it could be more than one home: keep it separate
            clusters.append([listing["id"]])
    for clusters in buildings.values():
        # A listing with no unit that also fits a home in another cluster (unit 1 here,
        # unit 2 there) cannot be pinned to either, so it stands alone.
        for cluster in list(clusters):
            others = [i for c in clusters if c is not cluster for i in c]
            for listing_id in [i for i in cluster if not homes[i].unit]:
                if len(cluster) > 1 and any(
                        same_home(by_id[listing_id], homes[listing_id], by_id[o], homes[o]) for o in others):
                    cluster.remove(listing_id)
                    clusters.append([listing_id])
        for cluster in clusters:
            for listing_id in cluster:
                out[listing_id]["group"] = cluster
                if listing_id != cluster[0]:
                    out[listing_id]["dup_of"] = cluster[0]
    for key, first in legacy.items():
        members = [i for i, o in out.items() if o["key"] == key and (i == first or o["dup_of"] == first)]
        for listing_id in members:
            out[listing_id]["group"] = members
    return out
