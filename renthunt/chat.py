"""The chat assistant: a local model (Ollama) with tools over the listings database."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import AsyncIterator

import httpx

from . import actions, ai, db, hunt

MAX_ROUNDS = 6
HISTORY_MESSAGES = 16

SYSTEM = """You are the assistant inside RentHunt, a personal app that collects apartment and \
house rental listings in the Merrimack Valley of Massachusetts and scores them against the \
user's criteria. You help one person find a place to rent.

How to work:
- Everything you say about listings must come from a tool result. Call search_listings or \
get_listing before describing, comparing or recommending anything; never make up a listing, \
price or address.
- Every search you run is applied to the page's filter bar, and the page shows all the results \
as photo cards and as pins on the map, \
so do not repeat the whole result list. Say how many you found, then give your top two or \
three picks. Start each pick on its own line with its id marker, like "#123", followed by one \
or two plain sentences on why it is worth a look: what is included, how close it is, when it \
is available, anything to watch out for.
- The app replaces each "#123" marker with a card showing that listing's name, rent, bedrooms \
and town, and the user never sees the number. So never talk about listings by number, never \
repeat the listing's name, rent, bedroom count or town after the marker, and never mention scores. Write the \
way a helpful friend would describe an apartment.
- For "near", "close to" or "around" a street, address or landmark, use the `near` parameter \
of search_listings. A street name is not a town: "Chelmsford St" is a street, "Chelmsford" is \
a town. Tell the user which place you searched around.
- When the user tells you what they want (budget, bedrooms, towns, pets, features), save it \
with update_criteria or set_towns right away, then say in one sentence what you changed and \
how many listings now match. "A 2 bedroom" means min_beds 2 and max_beds 2; "under $2,000" \
means max_price 2000. "Include 3 bedrooms", "also 3 bedrooms" or "up to 3 bedrooms" widens the \
range: change only the end that has to move (here max_beds 3) and leave the other as it is.
- Describe each listing with the numbers from the tool result, even when they differ from what \
the user asked for.
- When they react to a listing ("love it", "not for me", "I emailed them"), record it with \
update_listing.
- You can draft a short, friendly inquiry message to a landlord when asked; base it on the \
listing details, sign it "Brian", and work in the user's situation from the criteria below \
(for example how soon they need to move and how many cats they have), asking the landlord to \
confirm those points.
- When the criteria say the user must move immediately, lead with places that are available \
now, and say plainly when a place's move-in date or pet policy is not stated.
- Listings marked with a "possible scam" flag deserve a plain warning.
- Keep answers short and concrete. Ask a question only when you cannot proceed without it.

Saved people and places the user wants to be near: {places}. To find listings near one of \
them, pass its name as `near` (for example near="Mom").

Today is {today}.

Current search criteria:
{criteria}

Right now: {matches} matching listings, {new} of them not yet looked at."""


def _fn(name: str, description: str, properties: dict, required: list[str] = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)},
    }}


_num = {"type": "number"}
_strs = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    _fn("search_listings",
        "Search the stored rental listings. Returns compact rows; call get_listing for full details.",
        {
            "view": {"type": "string", "enum": ["matches", "new", "favorites", "pipeline", "all", "hidden"],
                     "description": "matches = fits the criteria (default); new = matches not yet looked at; "
                                    "pipeline = contacted, touring or applied; all = everything stored"},
            "text": {"type": "string", "description": "Word or phrase to find in the title, description or notes"},
            "towns": {**_strs, "description": "Only these towns"},
            "near": {"type": "string", "description": "Street, address or landmark to search around, "
                     "e.g. 'Chelmsford St, Lowell'. Results come back nearest first with distances."},
            "radius_miles": {**_num, "description": "With near: how far out to look, default 2"},
            "min_price": _num, "max_price": _num, "min_beds": _num, "max_beds": _num,
            "new_since_hours": {**_num, "description": "Only listings first seen in the last N hours"},
            "available_now": {"type": "boolean", "description": "Only places that can be moved into right away"},
            "cats_ok": {"type": "boolean", "description": "Only places that state cats are allowed"},
            "sort": {"type": "string", "enum": ["score", "newest", "price_asc", "price_desc"]},
            "limit": {**_num, "description": "Rows to return, default 10, at most 20"},
        }),
    _fn("get_listing", "Full details of one listing: description, amenities, address, contact, notes, price history.",
        {"id": {"type": "integer"}}, ["id"]),
    _fn("update_listing", "Record the user's decision or notes on a listing.",
        {
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": list(db.STATUSES),
                       "description": "seen, contacted, touring, applied, or rejected (not interested)"},
            "favorite": {"type": "boolean"},
            "notes": {"type": "string", "description": "Replaces the listing's notes"},
        }, ["id"]),
    _fn("get_settings", "Current criteria, towns, schedule, watched pages, and status of the last and next hunt.", {}),
    _fn("update_criteria",
        "Change search criteria. Send only the fields that change. List fields replace the whole list. "
        "For an exact size such as 'a 2 bedroom', set both min_beds and max_beds to 2. To include "
        "another size as well ('also 3 bedrooms'), move only one end of the range.",
        {
            "min_price": _num, "max_price": _num, "min_beds": _num, "max_beds": _num, "min_baths": _num,
            "cats": {"type": "boolean", "description": "Require that cats are allowed"},
            "dogs": {"type": "boolean", "description": "Require that dogs are allowed"},
            "must_have": {**_strs, "description": "Phrases a listing must mention"},
            "nice_to_have": {**_strs, "description": "Phrases that raise a listing's score"},
            "exclude": {**_strs, "description": "Phrases that rule a listing out"},
            "max_age_days": _num,
            "notes": {"type": "string", "description": "Free-text preferences to remember"},
            "cat_count": {**_num, "description": "How many cats the user has"},
            "move_in": {"type": "string", "enum": ["any", "asap"], "description": "asap = must move right away"},
        }),
    _fn("set_towns", "Turn towns on or off for the search, or add a new town by ZIP code.",
        {
            "enable": _strs, "disable": _strs,
            "add": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "zip": {"type": "string"}}, "required": ["name", "zip"]}},
        }),
    _fn("update_schedule", "Change how often the automatic hunt runs.",
        {
            "enabled": {"type": "boolean"},
            "interval_hours": _num,
            "quiet_start": {**_num, "description": "Hour (0-23) when automatic runs pause"},
            "quiet_end": {**_num, "description": "Hour (0-23) when they resume"},
        }),
    _fn("run_hunt", "Start a fresh search of all sources now. It runs in the background for a few minutes.", {}),
    _fn("add_watch_page",
        "Add a web page (for example a local landlord's vacancies page) to check on every hunt.",
        {"url": {"type": "string"}, "label": {"type": "string"}}, ["url"]),
    _fn("market_stats", "Median and lowest advertised rent by town and bedroom count.", {}),
]

TOOL_LABELS = {
    "search_listings": "Searching listings", "get_listing": "Reading listing",
    "update_listing": "Updating listing", "get_settings": "Checking settings",
    "update_criteria": "Updating criteria", "set_towns": "Updating towns",
    "update_schedule": "Updating schedule", "run_hunt": "Starting a hunt",
    "add_watch_page": "Adding watched page", "market_stats": "Crunching rents",
}
MUTATING = {"update_listing", "update_criteria", "set_towns", "update_schedule", "run_hunt", "add_watch_page"}


def _compact(listing: dict) -> dict:
    beds = listing["beds_min"]
    if listing["beds_max"] is not None and listing["beds_max"] != beds:
        beds = f"{listing['beds_min']:g}-{listing['beds_max']:g}"
    row = {
        "id": listing["id"], "title": listing["title"][:70], "town": listing["town"],
        "rent": listing["match_price"] or listing["price"], "beds": beds, "baths": listing["baths"],
        "source": listing["source"], "status": listing["status"],
        "first_seen": listing["first_seen"][:10], "highlights": listing["reasons"][1:4],
        "move_in": listing.get("avail"), "cats": listing.get("cats_ok"),
        "features": (listing["amenities"] or [])[:6],
    }
    if listing["favorite"]:
        row["favorite"] = True
    if listing.get("unverified"):
        row["availability"] = "unverified: its source could not be checked on the last hunt"
    if listing["flags"]:
        row["flags"] = listing["flags"]
    if listing["available"]:
        row["available"] = listing["available"]
    return {k: v for k, v in row.items() if v not in (None, [], "")}


def _opt(args: dict, key: str, kind):
    value = args.get(key)
    if value in (None, ""):
        return None
    return kind(float(value)) if kind in (int, float) else value


def run_tool(name: str, args: dict) -> dict:
    """Execute one tool call. Problems come back as {"error": ...} for the model to read."""
    try:
        if name == "search_listings":
            towns = args.get("towns")
            if isinstance(towns, str):
                towns = [towns]
            known = {t["name"].lower(): t["name"] for t in db.get_settings()["towns"]}
            towns = [known[str(t).lower()] for t in towns or [] if str(t).lower() in known] or None
            limit = min(20, max(1, _opt(args, "limit", int) or 10))
            near = (args.get("near") or "").strip() or None
            filters = dict(
                view=args.get("view") or "matches", text=args.get("text") or None, towns=towns,
                min_price=_opt(args, "min_price", int), max_price=_opt(args, "max_price", int),
                min_beds=_opt(args, "min_beds", float), max_beds=_opt(args, "max_beds", float),
                new_since_hours=_opt(args, "new_since_hours", float),
                available_now=bool(args.get("available_now")), cats_ok=bool(args.get("cats_ok")),
            )
            sort = args.get("sort") or "score"
            radius = _opt(args, "radius_miles", float)
            found = actions.search(near=near, radius_miles=radius, sort=sort, limit=limit, **filters)
            note = None
            if near and not found["total"]:
                radius = (found["center"]["radius"]) * 3
                found = actions.search(near=near, radius_miles=radius, sort=sort, limit=limit, **filters)
                note = f"Nothing that close, so the search was widened to {radius:g} miles."
            rows = []
            for listing in found["listings"]:
                row = _compact(listing)
                if "distance" in listing:
                    row["distance_miles"] = listing["distance"]
                rows.append(row)
            out = {"total_found": found["total"], "showing": len(rows), "listings": rows}
            if found["center"]:
                out["searched_around"] = found["center"]["label"]
            if note:
                out["note"] = note
            # The page mirrors this search in its filter bar and shows every result.
            shown = {k: v for k, v in {**filters, "near": near, "sort": sort,
                                       "radius_miles": found["center"]["radius"] if found["center"] else None,
                                       }.items() if v not in (None, False, "") and k != "new_since_hours"}
            if filters["new_since_hours"] is None:
                out["_ui"] = {"filters": shown}
            return out
        if name == "get_listing":
            listing = db.get_listing(int(args["id"]))
            if not listing:
                return {"error": f"there is no listing #{args['id']}"}
            keep = ("id", "title", "url", "source", "town", "address", "baths", "sqft", "units", "amenities",
                    "contact", "posted_at", "available", "first_seen", "active", "score", "is_match",
                    "reasons", "flags", "status", "favorite", "notes", "price_history")
            out = {k: listing[k] for k in keep if listing[k] not in (None, [], "")}
            out["description"] = (listing["description"] or "")[:3000]
            return out
        if name == "update_listing":
            listing = db.update_listing(
                int(args["id"]), status=args.get("status"), favorite=args.get("favorite"), notes=args.get("notes"))
            if not listing:
                return {"error": f"there is no listing #{args['id']}"}
            return {"ok": True, "id": listing["id"], "status": listing["status"],
                    "favorite": listing["favorite"], "notes": listing["notes"]}
        if name == "get_settings":
            settings = db.get_settings()
            return {
                "criteria": settings["criteria"],
                "towns_enabled": [t["name"] for t in settings["towns"] if t["enabled"]],
                "towns_available": [t["name"] for t in settings["towns"] if not t["enabled"]],
                "schedule": settings["schedule"],
                "sources": {k: v["enabled"] for k, v in settings["sources"].items()},
                "watched_pages": [{"url": w["url"], "label": w["label"], "state": w.get("state"),
                                   "last_status": w["last_status"], "last_good_check": w.get("last_success")}
                                  for w in actions.list_watches()],
                **{k: v for k, v in actions.overview().items() if k != "ai"},
            }
        if name == "update_criteria":
            criteria = actions.update_criteria(args)
            return {"ok": True, "criteria": criteria, "matching_listings_now": db.counts()["matches"]}
        if name == "set_towns":
            result = actions.set_towns(args.get("enable") or [], args.get("disable") or [], args.get("add") or [])
            return {**result, "matching_listings_now": db.counts()["matches"]}
        if name == "update_schedule":
            schedule = actions.update_schedule(args)
            due = hunt.next_run_time()
            return {"ok": True, "schedule": schedule, "next_run": due.isoformat(timespec="minutes") if due else None}
        if name == "run_hunt":
            started = actions.start_hunt("chat")
            return {"started": started, "note": "A hunt takes a few minutes; results appear in the app when it ends."
                    if started else "A hunt is already running."}
        if name == "add_watch_page":
            watch = actions.add_watch(args.get("url", ""), args.get("label") or "")
            return {"ok": True, "watching": watch["url"], "note": "It will be read on the next hunt."}
        if name == "market_stats":
            return {"rents": actions.market_stats()}
        return {"error": f"unknown tool {name}"}
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------- conversation storage ----------

def load_history(session_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT content FROM chat_messages WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    return [json.loads(r["content"]) for r in rows]


def _save(session_id: str, message: dict) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, message["role"], json.dumps(message), db.now()),
        )


def clear_history(session_id: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))


def _window(history: list[dict]) -> list[dict]:
    """The most recent messages, starting on a user turn so tool calls stay paired."""
    recent = history[-HISTORY_MESSAGES:]
    for index, message in enumerate(recent):
        if message["role"] == "user":
            return recent[index:]
    return recent[-1:]


def _system(settings: dict) -> dict:
    counts = db.counts()
    return {"role": "system", "content": SYSTEM.format(
        places=", ".join(f"{p['name']} ({p['label']})" for p in settings["places"]) or "none yet",
        today=datetime.now().strftime("%A, %B %-d, %Y"), criteria=ai.criteria_brief(settings),
        matches=counts["matches"], new=counts["new"])}


async def respond(session_id: str, text: str) -> AsyncIterator[dict]:
    """Run one user turn, yielding UI events: text, tool, refresh, error, done."""
    settings = db.get_settings()
    user = {"role": "user", "content": text}
    _save(session_id, user)
    messages = _window(load_history(session_id))
    changed = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)) as client:
            for round_number in range(MAX_ROUNDS):
                payload = ai.chat_payload(settings, [_system(settings), *messages], stream=True)
                if round_number < MAX_ROUNDS - 1:
                    payload["tools"] = TOOLS  # the last round must answer in words
                content, calls = "", []
                async with client.stream("POST", f"{ai.base_url(settings)}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise RuntimeError(chunk["error"])
                        message = chunk.get("message") or {}
                        if message.get("content"):
                            content += message["content"]
                            yield {"type": "text", "delta": message["content"]}
                        calls += message.get("tool_calls") or []
                assistant = {"role": "assistant", "content": content}
                if calls:
                    assistant["tool_calls"] = calls
                messages.append(assistant)
                _save(session_id, assistant)
                if not calls:
                    break
                for call in calls:
                    function = call.get("function") or {}
                    name, args = function.get("name", ""), function.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    yield {"type": "tool", "name": name, "label": TOOL_LABELS.get(name, name)}
                    result = await asyncio.to_thread(run_tool, name, args if isinstance(args, dict) else {})
                    changed = changed or (name in MUTATING and "error" not in result)
                    ui = result.pop("_ui", None)
                    if ui:
                        yield {"type": "filters", **ui}
                    reply = {"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)}
                    messages.append(reply)
                    _save(session_id, reply)
    except Exception as exc:
        yield {"type": "error", "message": ai.describe_error(exc)}
    if changed:
        yield {"type": "refresh"}
    yield {"type": "done"}
