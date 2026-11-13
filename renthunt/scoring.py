"""Decide whether a listing fits the criteria and how well (0-100)."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

# A phrase in the criteria also counts when one of these appears in the listing.
SYNONYMS = {
    "laundry": ["washer", "w/d", "laundromat on site"],
    "parking": ["garage", "off-street", "off street", "carport", "driveway"],
    "heat included": ["heat incl", "heat & hot water", "heat and hot water", "heat/hot water",
                      "heat + hot water", "utilities included", "all utilities"],
    "hot water included": ["hot water incl", "heat & hot water", "heat and hot water",
                           "heat/hot water", "utilities included", "all utilities", "rent includes"],
    "dishwasher": ["d/w"],
    "air conditioning": ["a/c", "central air", "air conditioned"],
    "yard": ["backyard", "back yard"],
}
SCAM_PHRASES = ["western union", "money order", "wire transfer", "out of the country",
                "out of state for work", "god bless", "send the keys", "mail you the keys",
                "deposit before viewing", "gift card"]
NO_PETS = ["no pets", "pets not allowed", "no animals", "pet free", "no pet"]
PET_OK = {
    "cats": ["cats are ok", "cat ok", "cats ok", "cats allowed", "cat friendly", "cats only",
             "pet friendly", "pets allowed", "pets ok", "pets welcome", "cats welcome"],
    "dogs": ["dogs are ok", "dog ok", "dogs ok", "dogs allowed", "dog friendly",
             "pet friendly", "pets allowed", "pets ok", "pets welcome", "dogs welcome"],
}
PET_NO = {"cats": ["no cats", "dogs only"], "dogs": ["no dogs", "cats only", "cat only"]}


ONE_CAT = re.compile(
    r"\b(one|1|single)\s+(cat|pet)\b|\bmax(imum)?\.?\s*(of\s*)?(1|one)\s*(cat|pet)|"
    r"\b(cat|pet)s?\s*(limit|max)\w*\s*(of|to|:)?\s*(1|one)\b|\b(1|one)\s+(cat|pet)\s+(max|only|limit)")
AVAILABLE_NOW = ["available now", "avail now", "available immediately", "immediate occupancy",
                 "immediate move", "move in today", "move-in ready", "move in ready", "ready now",
                 "available today", "available asap", "vacant"]
DATE_IN_TEXT = re.compile(r"avail(?:able)?(?:\s+date)?\s*[:\-]?\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)")


def _parse_day(text: str, today: date) -> date | None:
    """Read 'dec 1', 'December 1st', '12/01', '09/15/2026' and the like."""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip().lower()).replace(",", " ")
    text = " ".join(text.split())
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    for fmt in ("%m/%d", "%b %d", "%B %d"):
        try:
            day = datetime.strptime(f"{text} 2000", f"{fmt} %Y").date().replace(year=today.year)
        except ValueError:
            continue
        # A bare "jan 1" seen in November means next January.
        return day.replace(year=today.year + 1) if (today - day).days > 120 else day
    return None


def availability(listing: dict, today: date | None = None) -> str:
    """'now', 'soon' (within about a month), 'later', or 'unknown'."""
    today = today or date.today()
    raw = (listing.get("available") or "").lower().strip()
    text = f"{listing.get('title') or ''} {(listing.get('description') or '')[:2000]}".lower()
    day = None
    if raw:
        if any(word in raw for word in ("now", "today", "immediate", "unit")):
            return "now"
        day = _parse_day(raw, today)
    if day is None:
        found = DATE_IN_TEXT.search(text)
        day = _parse_day(found.group(1), today) if found else None
    if day is not None:
        wait = (day - today).days
        return "now" if wait <= 3 else "soon" if wait <= 35 else "later"
    return "now" if any(p in text for p in AVAILABLE_NOW) else "unknown"


def cat_policy(text: str) -> str:
    """'yes', 'one' (a one-cat limit is mentioned), 'no', or 'unknown'."""
    if any(p in text for p in PET_NO["cats"] + NO_PETS):
        return "no"
    if ONE_CAT.search(text):
        return "one"
    return "yes" if any(p in text for p in PET_OK["cats"]) else "unknown"


def _has(text: str, phrase: str) -> bool:
    phrase = phrase.lower().strip()
    if not phrase:
        return False
    return any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text) for p in [phrase, *SYNONYMS.get(phrase, [])])


def _age_days(listing: dict) -> float | None:
    stamp = listing.get("posted_at") or listing.get("first_seen")
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400)


def beds_label(beds: float | None) -> str:
    if beds is None:
        return "?"
    return "studio" if beds == 0 else f"{beds:g}"


def evaluate(listing: dict, settings: dict, medians: dict | None = None) -> dict:
    """Returns {is_match, match_price, score, reasons, flags}."""
    c = settings["criteria"]
    enabled = [t["name"] for t in settings["towns"] if t.get("enabled")]
    text = " ".join([
        listing.get("title") or "", listing.get("description") or "",
        " ".join(listing.get("amenities") or []),
    ]).lower()
    good, bad, flags = [], [], []

    # --- hard requirements ---
    if listing.get("town") not in enabled:
        bad.append(f"{listing.get('town') or 'unknown town'} is not one of your towns")

    lo, hi = c.get("min_price") or 0, c.get("max_price") or 10**9
    fits = [
        u for u in listing.get("units") or []
        if u.get("price_low") is not None and lo <= u["price_low"] <= hi
        and (u.get("beds") is None or c["min_beds"] <= u["beds"] <= c["max_beds"])
    ]
    match_price = min((u["price_low"] for u in fits), default=None)
    if match_price is None:
        if not any(u.get("price_low") is not None for u in listing.get("units") or []):
            bad.append("no rent listed")
        else:
            bad.append("no unit fits your rent and bedroom range")
    else:
        best = min(fits, key=lambda u: u["price_low"])
        beds = best.get("beds")
        shape = "" if beds is None else " studio" if beds == 0 else f" for {beds:g} bed"
        good.append(f"${match_price:,}{shape}")

    if c.get("min_baths") and listing.get("baths") is not None and listing["baths"] < c["min_baths"]:
        bad.append(f"only {listing['baths']:g} bath")

    for phrase in c.get("exclude") or []:
        if _has(text, phrase):
            bad.append(f'mentions "{phrase}"')

    has_details = bool(listing.get("description"))
    for phrase in c.get("must_have") or []:
        if _has(text, phrase):
            good.append(f"has {phrase}")
        elif has_details:
            bad.append(f'no mention of "{phrase}"')

    pets_confirmed = 0
    cats = cat_policy(text)
    cat_count = int(c.get("cat_count") or 1)
    cat_penalty = 0
    if c.get("cats"):
        if cats == "no":
            bad.append("cats not allowed")
        elif cats == "one" and cat_count >= 2:
            flags.append(f"may allow only one cat (you have {cat_count})")
            cat_penalty = 12
        elif cats in ("yes", "one"):
            good.append("cats OK")
            pets_confirmed += 1
            if cat_count >= 2:
                flags.append(f"confirm {cat_count} cats are OK")
        else:
            flags.append("pet policy unknown")
    if c.get("dogs"):
        if any(p in text for p in PET_NO["dogs"] + NO_PETS):
            bad.append("dogs not allowed")
        elif any(p in text for p in PET_OK["dogs"]):
            good.append("dogs OK")
            pets_confirmed += 1
        else:
            flags.append("pet policy unknown")

    avail = availability(listing)
    asap = c.get("move_in") == "asap"
    if asap and avail == "later":
        bad.append(f"not available until {listing.get('available') or 'later'}")
    elif avail == "now":
        good.append("available now")

    age = _age_days(listing)
    if listing.get("posted_at") and age is not None and c.get("max_age_days") and age > c["max_age_days"]:
        bad.append(f"posted {int(age)} days ago")

    # --- score ---
    score = 0.0
    if match_price is not None:
        floor = max(lo, 0.5 * hi) if hi < 10**9 else lo
        span = max(1.0, hi - floor)
        score += 40 * min(1.0, max(0.0, (hi - match_price) / span))
    if listing.get("town") in enabled:
        score += 15 * (1 - enabled.index(listing["town"]) / max(1, len(enabled)))
    nice = c.get("nice_to_have") or []
    hits = [p for p in nice if _has(text, p)]
    score += 20 * len(hits) / len(nice) if nice else 10
    if hits:
        good.append(", ".join(hits))
    if age is not None:
        score += 15 if age <= 1 else 12 if age <= 3 else 8 if age <= 7 else 4 if age <= 14 else 0
    score += 5 if listing.get("images") else 0
    score += 5 if (listing.get("sqft") or len(listing.get("description") or "") > 200) else 0
    score += 2.5 * pets_confirmed - cat_penalty
    if asap:  # when the move is urgent, being ready now matters more than polish
        score += {"now": 15, "soon": 4, "unknown": -6}.get(avail, 0)
        if avail == "unknown":
            flags.append("move-in date not stated")
    score = max(0.0, min(100.0, score))

    # --- warnings ---
    if any(p in text for p in SCAM_PHRASES):
        flags.append("possible scam: suspicious wording")
    if match_price is not None and medians:
        best_beds = min(fits, key=lambda u: u["price_low"]).get("beds")
        median = medians.get(best_beds)
        if median and match_price < 0.55 * median:
            flags.append(f"possible scam: rent far below the ${int(median):,} median")
    if listing.get("price_prev") and listing.get("price") and listing["price"] < listing["price_prev"]:
        flags.append(f"price drop: was ${listing['price_prev']:,}")

    if any(f.startswith("possible scam") for f in flags):
        score *= 0.5  # still shown, but well below honest-looking listings
    is_match = not bad
    return {
        "is_match": is_match,
        "match_price": match_price,
        "score": round(score) if is_match else 0,
        "reasons": good if is_match else bad,
        "flags": sorted(set(flags)),
        "avail": avail,
        "cats_ok": cats,
    }
