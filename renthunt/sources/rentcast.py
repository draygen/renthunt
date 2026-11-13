"""RentCast long-term rental listings (optional, paid API with a small free tier).

Endpoint, per https://developers.rentcast.io/reference/rental-listings-long-term :

    GET https://api.rentcast.io/v1/listings/rental/long-term
    header  X-Api-Key: <key>
    query   city, state, status=Active, price=min:max, bedrooms=min:max, limit (max 500), offset

Answers: 200 with a JSON array; 404 when nothing matches (an empty result, not a failure);
401 for a bad key; 403 for key restrictions or a billing problem; 429 above 20 requests a
second; 500/504 on their side.

Every request counts against your RentCast plan, so this source is off by default and
spends from three budgets you set: requests per hunt, requests per billing month, and a
minimum number of hours between checks of the same town. When a hunt cannot cover every
town, the towns checked longest ago go first.

The key comes from the RENTCAST_API_KEY environment variable (or the project's .env file).
It is sent only in the request header and is never stored, logged, or returned by the API.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import httpx

from .. import db
from .base import Fetcher, Log, SourceResult, assign_town, enabled_towns

API_URL = "https://api.rentcast.io/v1/listings/rental/long-term"
KEY_ENV = "RENTCAST_API_KEY"
PAGE_SIZE = 500  # the API's maximum
USAGE_STATE = "rentcast_usage"      # {"period": "2026-11-01", "count": 12}
CHECKED_STATE = "rentcast_checked"  # {"town:Lowell": "2026-11-13T10:00:00+00:00"}


class StopSource(RuntimeError):
    """A problem that will hit every further request this run (bad key, plan, rate limit)."""


def api_key() -> str:
    return (os.environ.get(KEY_ENV) or "").strip()


def _scrub(message: str) -> str:
    key = api_key()
    return message.replace(key, "[key]") if key else message


def period_start(billing_day: int, today: date | None = None) -> str:
    """First day of the current billing month, given the day of the month it renews on."""
    today = today or datetime.now(timezone.utc).date()
    day = min(28, max(1, int(billing_day or 1)))
    start = today.replace(day=day)
    if start > today:
        start = (start.replace(day=1) - timedelta(days=1)).replace(day=day)
    return start.isoformat()


def usage(cfg: dict) -> dict:
    """Requests sent in the current billing month: {"period", "count"}."""
    period = period_start(cfg.get("billing_day", 1))
    stored = db.get_state(USAGE_STATE) or {}
    return {"period": period, "count": int(stored.get("count", 0)) if stored.get("period") == period else 0}


def status(settings: dict) -> dict:
    """What the settings screen may know. Never includes the key itself."""
    cfg = settings["sources"]["rentcast"]
    used = usage(cfg)
    return {"key_present": bool(api_key()), "key_env": KEY_ENV, "used": used["count"],
            "period_start": used["period"], "monthly_budget": int(cfg.get("max_requests_per_month", 0))}


def _range(low, high) -> str | None:
    if low in (None, "") and high in (None, ""):
        return None
    return f"{low if low not in (None, '') else '*'}:{high if high not in (None, '') else '*'}"


def to_item(record: dict, settings: dict, scope: str) -> dict | None:
    if not record.get("id") or not record.get("price"):
        return None
    town = assign_town(settings["towns"], record.get("city"))
    if not town or (record.get("state") and town.get("state") and record["state"] != town["state"]):
        return None
    street = " ".join(p for p in (record.get("addressLine1"), record.get("addressLine2")) if p)
    address = record.get("formattedAddress") or ", ".join(
        p for p in (street, record.get("city"), f"{record.get('state') or ''} {record.get('zipCode') or ''}".strip()) if p)
    agent, office = record.get("listingAgent") or {}, record.get("listingOffice") or {}
    kind = record.get("propertyType")
    facts = [kind, f"built {record['yearBuilt']}" if record.get("yearBuilt") else None,
             f"listed by {office['name']}" if office.get("name") else None,
             f"MLS {record['mlsName']} #{record['mlsNumber']}" if record.get("mlsNumber") and record.get("mlsName") else None,
             f"{record['daysOnMarket']} days on the market" if record.get("daysOnMarket") is not None else None]
    contact = ", ".join(dict.fromkeys(
        p for p in (agent.get("name"), agent.get("phone") or office.get("phone"),
                    agent.get("email") or office.get("email")) if p))
    beds = record.get("bedrooms")
    sqft = int(record["squareFootage"]) if record.get("squareFootage") else None
    # RentCast has no page for a listing. Link to the agent's or office's site when it
    # gives one, otherwise to a map of the address.
    site = agent.get("website") or office.get("website") or ""
    url = site if site.startswith("http") else str(httpx.URL(
        "https://www.google.com/maps/search/", params={"api": "1", "query": address}))
    return {
        "source": "rentcast",
        "source_id": str(record["id"]),
        "scope": scope,
        "url": url,
        "title": (street or address)[:200],
        "town": town["name"],
        "state": town.get("state", "MA"),
        "address": address,
        "lat": record.get("latitude"), "lng": record.get("longitude"),
        "units": [{"beds": float(beds) if beds is not None else None, "price_low": int(record["price"]),
                   "price_high": None, "sqft": sqft}],
        "baths": record.get("bathrooms"),
        "sqft": sqft,
        "description": " · ".join(str(f) for f in facts if f),
        "contact": contact or None,
        "posted_at": record.get("listedDate"),
    }


def _get(client: httpx.Client, params: dict) -> list[dict]:
    """One request. Returns the records; raises StopSource or RuntimeError with a plain reason."""
    try:
        resp = client.get(API_URL, params=params)
    except httpx.TimeoutException as exc:
        raise RuntimeError("RentCast took too long to answer") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach RentCast ({type(exc).__name__})") from exc
    code = resp.status_code
    if code == 404:
        return []  # documented as "no results"
    if code == 401:
        raise StopSource(f"RentCast rejected the API key (HTTP 401). Check {KEY_ENV}.")
    if code == 403:
        raise StopSource("RentCast refused the request (HTTP 403): the key is restricted, the plan's "
                         "request quota is used up, or there is a billing problem. See your RentCast dashboard.")
    if code == 429:
        raise StopSource("RentCast says too many requests (HTTP 429); stopped for this hunt.")
    if code != 200:
        raise RuntimeError(f"RentCast answered HTTP {code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError("RentCast sent an unreadable answer") from exc
    if not isinstance(data, list):
        raise RuntimeError("RentCast sent an answer in an unexpected shape")
    return data


def fetch(settings: dict, fetcher: Fetcher | None, log: Log,
          transport: httpx.BaseTransport | None = None, now: datetime | None = None) -> SourceResult:
    cfg = settings["sources"]["rentcast"]
    criteria = settings["criteria"]
    result = SourceResult()
    towns = enabled_towns(settings)
    scopes = {f"town:{t['name']}": t for t in towns}
    result.labels = {scope: t["name"] for scope, t in scopes.items()}
    key = api_key()
    if not key:
        # Switched on but unusable: say so loudly rather than report a clean hunt.
        raise RuntimeError(f"no API key. Set {KEY_ENV} (see README: RentCast) or switch RentCast off.")

    now = now or datetime.now(timezone.utc)
    used = usage(cfg)
    month_left = max(0, int(cfg.get("max_requests_per_month", 45)) - used["count"])
    budget = min(max(0, int(cfg.get("max_requests_per_hunt", 8))), month_left)
    max_pages = max(1, int(cfg.get("max_pages_per_town", 2)))
    wait_hours = float(cfg.get("hours_between_checks", 168))
    checked_at: dict = db.get_state(CHECKED_STATE) or {}

    def last(scope: str) -> datetime:
        try:
            return datetime.fromisoformat(checked_at[scope])
        except (KeyError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    due = sorted((s for s in scopes if now - last(s) >= timedelta(hours=wait_hours)), key=last)
    for scope in scopes:
        if scope not in due:
            result.skipped[scope] = f"checked within the last {wait_hours:g} hours"
    if not due:
        result.note = "no town is due for a check yet"
        log(f"rentcast: {result.note}")
        return result
    if month_left == 0:
        result.note = (f"monthly request budget used up ({used['count']} of "
                       f"{cfg.get('max_requests_per_month')} since {used['period']})")
        result.skipped.update({scope: "monthly request budget used up" for scope in due})
        log(f"rentcast: skipped, {result.note}")
        return result

    params = {"status": "Active", "limit": PAGE_SIZE}
    price = _range(criteria.get("min_price") or None, criteria.get("max_price") or None)
    beds = _range(int(criteria["min_beds"]) if criteria.get("min_beds") is not None else None,
                  int(criteria["max_beds"]) if criteria.get("max_beds") is not None else None)
    if price:
        params["price"] = price
    if beds:
        params["bedrooms"] = beds

    sent = troubles = 0
    items: dict[str, dict] = {}
    stopped: str | None = None
    client = httpx.Client(headers={"X-Api-Key": key, "Accept": "application/json"},
                          timeout=30, transport=transport)
    try:
        for scope in due:
            town = scopes[scope]
            if stopped:
                result.failed[scope] = stopped
                continue
            if sent >= budget:
                result.skipped[scope] = "request budget for this hunt used up"
                continue
            found, complete = 0, False
            try:
                for page in range(max_pages):
                    if sent >= budget:
                        result.failed[scope] = "request budget ran out before every page was read"
                        break
                    sent += 1
                    records = _get(client, {**params, "city": town["name"], "state": town.get("state", "MA"),
                                            "offset": page * PAGE_SIZE})
                    for record in records:
                        item = to_item(record, settings, scope)
                        if item:
                            found += 1
                            items.setdefault(item["source_id"], item)
                    if len(records) < PAGE_SIZE:
                        complete = True
                        break
                else:
                    result.failed[scope] = f"more than {max_pages * PAGE_SIZE} results; only the first {max_pages} page(s) were read"
            except StopSource as exc:
                stopped = _scrub(str(exc))
                result.failed[scope] = stopped
                log(f"rentcast: {stopped}")
                continue
            except RuntimeError as exc:
                result.failed[scope] = _scrub(str(exc))
                log(f"rentcast: {town['name']} failed: {result.failed[scope]}")
                troubles += 1
                if troubles >= 2:  # do not spend the budget on a service that is down
                    stopped = "RentCast kept failing; stopped for this hunt"
                continue
            troubles = 0
            if complete:
                result.checked.add(scope)
                checked_at[scope] = now.isoformat(timespec="seconds")
            log(f"rentcast: {town['name']}: {found} listings" + ("" if complete else " (not read in full)"))
    finally:
        client.close()
        if sent:
            db.set_state(USAGE_STATE, {"period": used["period"], "count": used["count"] + sent})
            db.set_state(CHECKED_STATE, checked_at)
    result.items = list(items.values())
    waiting = sum(1 for reason in result.skipped.values() if "budget" in reason)
    result.note = (f"{sent} request(s) sent, {used['count'] + sent} of {cfg.get('max_requests_per_month')} "
                   f"this billing month" + (f"; {waiting} town(s) wait for the next hunt" if waiting else ""))
    log(f"rentcast: {result.note}")
    return result
