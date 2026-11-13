"""The hunt: fetch every enabled source, store, de-duplicate, score, alert."""
from __future__ import annotations

import json
import re
import statistics
import threading
from datetime import datetime, timedelta

import httpx

from . import ai, db, dedupe, scoring
from .sources import ai_sources, craigslist, mail, rentcast, rentcom, render
from .sources.base import Fetcher, SourceResult

_lock = threading.Lock()
progress: dict = {"running": False, "step": "", "started_at": None}

# Fields a later fetch may overwrite only with a real value (search pages lack them).
KEEP_IF_EMPTY = ("description", "images", "amenities", "address", "lat", "lng", "baths",
                 "sqft", "contact", "posted_at", "available", "town", "state")


def is_running() -> bool:
    return progress["running"]


def _prices(units: list[dict]) -> tuple[int | None, int | None]:
    lows = [u["price_low"] for u in units if u.get("price_low") is not None]
    highs = [u.get("price_high") or u["price_low"] for u in units if u.get("price_low") is not None]
    return (min(lows), max(highs)) if lows else (None, None)


def _beds(units: list[dict]) -> tuple[float | None, float | None]:
    beds = [u["beds"] for u in units if u.get("beds") is not None]
    return (min(beds), max(beds)) if beds else (None, None)


def upsert(conn, item: dict, stamp: str) -> tuple[int, bool]:
    """Insert or refresh one listing. Returns (id, is_new)."""
    units = item.get("units") or []
    price, price_max = _prices(units)
    beds_min, beds_max = _beds(units)
    row = conn.execute(
        "SELECT * FROM listings WHERE source=? AND source_id=?", (item["source"], item["source_id"])
    ).fetchone()
    values = {
        "url": item["url"], "title": item["title"], "units": json.dumps(units),
        "price": price, "price_max": price_max, "beds_min": beds_min, "beds_max": beds_max,
        "last_seen": stamp, "missed_runs": 0, "active": 1, "unverified": 0,
    }
    if item.get("scope"):
        values["scope"] = item["scope"]
    for key in KEEP_IF_EMPTY:
        value = item.get(key)
        if isinstance(value, list):
            value = json.dumps(value) if value else None
        if value not in (None, ""):
            values[key] = value
        elif row is None:
            values[key] = None
    if row is None:
        values.update(source=item["source"], source_id=item["source_id"], first_seen=stamp)
        cols = ", ".join(values)
        cur = conn.execute(
            f"INSERT INTO listings ({cols}) VALUES ({', '.join('?' * len(values))})", list(values.values())
        )
        if price is not None:
            conn.execute("INSERT INTO price_history VALUES (?, ?, ?)", (cur.lastrowid, price, stamp))
        return cur.lastrowid, True
    if price is not None and row["price"] is not None and price != row["price"]:
        values["price_prev"] = row["price"]
        conn.execute("INSERT INTO price_history VALUES (?, ?, ?)", (row["id"], price, stamp))
    sets = ", ".join(f"{k}=?" for k in values)
    conn.execute(f"UPDATE listings SET {sets} WHERE id=?", [*values.values(), row["id"]])
    return row["id"], False


INHERIT_WITHIN_DAYS = 60


def _inherit_marks(conn, listings: list[dict], groups: dict[int, dict]) -> None:
    """Carry the owner's marks onto the copy that is now shown for a home.

    When the copy the owner favorited, noted, tracked or hid has gone from its source and
    another source still lists the same home, the surviving copy takes over those marks
    instead of turning up as a brand-new listing (and is not announced again).
    """
    recent = (datetime.now().astimezone() - timedelta(days=INHERIT_WITHIN_DAYS)).isoformat()
    donors: dict[tuple, list[tuple[dict, dedupe.Home]]] = {}
    for listing in listings:
        if not listing["active"] and dedupe.is_engaged(listing) and listing["last_seen"] >= recent[:10]:
            home = dedupe.home_of(listing)
            if home:
                donors.setdefault(home.building, []).append((listing, home))
    if not donors:
        return
    for listing in listings:
        if not listing["active"] or groups[listing["id"]]["dup_of"] is not None or dedupe.is_engaged(listing):
            continue
        home = dedupe.home_of(listing)
        matches = [d for d, d_home in donors.get(home.building, []) if dedupe.same_home(listing, home, d, d_home)] if home else []
        if not matches:
            continue
        donor = min(matches, key=lambda d: d["id"])
        marks = {"status": donor["status"], "favorite": int(donor["favorite"]), "hidden": int(donor["hidden"]),
                 "notes": donor["notes"], "alerted": 1}
        conn.execute("UPDATE listings SET status=?, favorite=?, hidden=?, notes=?, alerted=? WHERE id=?",
                     (*marks.values(), listing["id"]))
        listing.update(marks)


def rescore_all() -> None:
    """Re-evaluate every listing against the current settings and regroup duplicates."""
    settings = db.get_settings()
    with db.connect() as conn:
        listings = [db.row_to_listing(r) for r in conn.execute("SELECT * FROM listings")]
        by_beds: dict = {}
        for listing in listings:
            if listing["active"]:
                for unit in listing["units"]:
                    if unit.get("price_low") and unit.get("beds") is not None:
                        by_beds.setdefault(unit["beds"], []).append(unit["price_low"])
        medians = {b: statistics.median(p) for b, p in by_beds.items() if len(p) >= 8}

        groups = dedupe.group(listings)
        _inherit_marks(conn, listings, groups)
        for listing in listings:
            result = scoring.evaluate(listing, settings, medians)
            info = groups[listing["id"]]
            conn.execute(
                "UPDATE listings SET is_match=?, match_price=?, score=?, reasons=?, flags=?, "
                "avail=?, cats_ok=?, fingerprint=?, dup_of=? WHERE id=?",
                (int(result["is_match"]), result["match_price"], result["score"],
                 json.dumps(result["reasons"]), json.dumps(result["flags"]),
                 result["avail"], result["cats_ok"], info["key"], info["dup_of"], listing["id"]),
            )


def _settle(conn, source: str, result: SourceResult, stamp: str, limit: int) -> int:
    """Decide what this run says about each listing of `source` that it did not see.

    Seen in a search that ran in full and was not there: one more missed run (inactive
    after `limit`). Its search or page failed: untouched, flagged unverified. Its page was
    unchanged: counted as seen. Its search was not attempted: untouched. Returns how many
    listings were left unverified.
    """
    rows = conn.execute(
        "SELECT id, scope, missed_runs FROM listings WHERE source=? AND active=1 AND last_seen<?",
        (source, stamp),
    ).fetchall()
    known = result.checked | result.unchanged | set(result.failed) | set(result.skipped)
    unverified = 0
    for row in rows:
        scope = row["scope"]
        if scope in result.unchanged and row["missed_runs"] == 0:
            # On the page at its last good read, and the page has not changed since.
            conn.execute("UPDATE listings SET last_seen=?, missed_runs=0, unverified=0 WHERE id=?",
                         (stamp, row["id"]))
        elif scope in result.failed or (scope not in known and result.failed):
            # Its own search failed, or we cannot tell which search it belongs to and
            # some of them failed: no verdict.
            conn.execute("UPDATE listings SET unverified=1 WHERE id=?", (row["id"],))
            unverified += 1
        elif scope in result.skipped:
            continue
        else:  # checked and absent, or its search is no longer part of the hunt
            missed = row["missed_runs"] + 1
            conn.execute("UPDATE listings SET missed_runs=?, active=?, unverified=0 WHERE id=?",
                         (missed, int(missed < limit), row["id"]))
    return unverified


def _fetch_details(settings: dict, fetcher: Fetcher, log) -> None:
    limit = int(settings["sources"]["craigslist"].get("detail_fetch_limit", 25))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, url FROM listings WHERE source='craigslist' AND active=1 AND is_match=1 "
            "AND detail_fetched=0 AND dup_of IS NULL ORDER BY first_seen DESC LIMIT ?", (limit,),
        ).fetchall()
    fetched = gone = 0
    for row in rows:
        progress["step"] = f"Reading Craigslist post {fetched + gone + 1} of {len(rows)}"
        try:
            detail = craigslist.fetch_detail(row["url"], fetcher)
        except Exception as exc:
            log(f"craigslist: detail fetch failed for listing {row['id']}: {exc}")
            continue
        with db.connect() as conn:
            if detail is None:
                conn.execute("UPDATE listings SET active=0, detail_fetched=1 WHERE id=?", (row["id"],))
                gone += 1
                continue
            for key in ("images", "amenities"):
                if key in detail:
                    detail[key] = json.dumps(detail[key])
            detail["detail_fetched"] = 1
            sets = ", ".join(f"{k}=?" for k in detail)
            conn.execute(f"UPDATE listings SET {sets} WHERE id=?", [*detail.values(), row["id"]])
            fetched += 1
    if rows:
        log(f"craigslist: read {fetched} full posts ({gone} already removed)")


def _send_alerts(settings: dict, log) -> list[dict]:
    """Mark fresh matches as alerted and push them to the webhook if one is set."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE is_match=1 AND active=1 AND hidden=0 AND alerted=0 "
            "AND dup_of IS NULL AND score>=? ORDER BY score DESC", (settings["notify"]["min_score"],),
        ).fetchall()
        listings = [db.row_to_listing(r) for r in rows]
        # Listings below the bar are marked too, so a later rescore does not re-announce old posts.
        conn.execute("UPDATE listings SET alerted=1 WHERE alerted=0 AND active=1")
    url = (settings["notify"].get("webhook_url") or "").strip()
    if not listings or not url:
        return listings
    lines = [
        f"${l['match_price'] or l['price']:,} · {scoring.beds_label(l['beds_min'])} bd · {l['town']} · "
        f"score {l['score']} · {l['title'][:60]}\n{l['url']}"
        for l in listings[:8]
    ]
    more = f"\n…and {len(listings) - 8} more" if len(listings) > 8 else ""
    body = f"RentHunt: {len(listings)} new match(es)\n\n" + "\n\n".join(lines) + more
    try:
        if "discord.com/api/webhooks" in url or "hooks.slack.com" in url:
            key = "content" if "discord" in url else "text"
            resp = httpx.post(url, json={key: body[:1900]}, timeout=20)
        else:  # ntfy.sh and compatible: plain-text body
            resp = httpx.post(url, content=body.encode(), headers={"Title": "RentHunt"}, timeout=20)
        resp.raise_for_status()
        log(f"notify: sent {len(listings)} new matches")
    except Exception as exc:
        log(f"notify: webhook failed: {exc}")
    return listings


def run(trigger: str = "manual") -> dict | None:
    """Run one full hunt. Returns the run summary, or None if one is already running."""
    if not _lock.acquire(blocking=False):
        return None
    lines: list[str] = []

    def log(message: str) -> None:
        lines.append(f"{datetime.now():%H:%M:%S}  {message}")

    stamp = db.now()
    progress.update(running=True, step="Starting", started_at=stamp)
    with db.connect() as conn:
        run_id = conn.execute(
            "INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (stamp, trigger)
        ).lastrowid
    summary: dict = {"sources": {}, "new": 0, "new_matches": 0}
    status = "ok"
    settings = db.get_settings()
    fetcher = Fetcher(settings.get("request_delay_seconds", 2.5), log)
    browser = render.Browser(settings["sources"]["watch"].get("render_timeout_seconds", 30))
    try:
        sources = settings["sources"]
        plan = []
        if sources["craigslist"]["enabled"]:
            plan.append(("craigslist", "Searching Craigslist"))
        if sources["rentcom"]["enabled"]:
            plan.append(("rentcom", "Searching Rent.com"))
        if sources["rentcast"]["enabled"]:
            plan.append(("rentcast", "Asking RentCast"))
        if sources["mail"]["enabled"]:
            plan.append(("mail", "Reading alert emails"))
        with db.connect() as conn:
            watching = conn.execute("SELECT COUNT(*) FROM watch_urls WHERE enabled=1").fetchone()[0]
        if sources["watch"]["enabled"] and watching:
            plan.append(("watch", "Reading your watched pages"))

        limit = int(settings.get("inactive_after_missed_runs", 3))
        for name, label in plan:
            progress["step"] = label
            try:
                if name == "craigslist":
                    result = craigslist.fetch(settings, fetcher, log)
                elif name == "rentcom":
                    result = rentcom.fetch(settings, fetcher, log)
                elif name == "rentcast":
                    result = rentcast.fetch(settings, fetcher, log)
                elif name == "mail":
                    result = mail.fetch(settings, fetcher, log)
                else:
                    result = ai_sources.fetch_watch(settings, fetcher, log, browser)
                if not isinstance(result, SourceResult):  # a simple source: a plain list, all or nothing
                    result = SourceResult(items=list(result), checked={"*"})
                    for item in result.items:
                        item.setdefault("scope", "*")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"[:300]
                log(f"{name}: FAILED: {reason}")
                with db.connect() as conn:
                    left = conn.execute("UPDATE listings SET unverified=1 WHERE source=? AND active=1",
                                        (name,)).rowcount
                summary["sources"][name] = {"error": reason, "unverified": left}
                status = "partial"
                continue
            new = 0
            with db.connect() as conn:
                for item in result.items:
                    new += upsert(conn, item, stamp)[1]
                unverified = _settle(conn, name, result, stamp, limit)
            entry: dict = {"found": len(result.items), "new": new}
            if result.failed:
                entry["problems"] = result.problems()[:20]
                entry["unverified"] = unverified
                status = "partial"
                if not (result.checked or result.unchanged or result.items):
                    entry["error"] = "nothing could be checked"
                log(f"{name}: {len(result.failed)} search(es) or page(s) could not be checked; "
                    f"{unverified} existing listing(s) left unverified")
            if result.skipped:
                entry["skipped"] = len(result.skipped)
            if result.note:
                entry["note"] = result.note
            summary["sources"][name] = entry
            summary["new"] += new

        progress["step"] = "Scoring"
        rescore_all()
        if sources["craigslist"]["enabled"]:
            _fetch_details(settings, fetcher, log)
            rescore_all()
        alerts = _send_alerts(settings, log)
        summary["new_matches"] = len(alerts)
        summary["counts"] = db.counts()
        if summary["sources"] and all("error" in s for s in summary["sources"].values()):
            status = "failed"
        log(f"done: {summary['new']} new listings, {len(alerts)} new matches worth a look"
            + ("" if status == "ok" else "; parts of this hunt could not be checked (see above)"))
    except Exception as exc:
        status = "failed"
        log(f"hunt crashed: {type(exc).__name__}: {exc}")
    finally:
        fetcher.close()
        browser.close()
        with db.connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, summary=?, log=? WHERE id=?",
                (db.now(), status, json.dumps(summary), "\n".join(lines), run_id),
            )
        progress.update(running=False, step="")
        _lock.release()
    return {"id": run_id, "status": status, **summary}


# ---------- schedule ----------

def last_run() -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    out = dict(row)
    out["summary"] = json.loads(out["summary"]) if out["summary"] else {}
    return out


def _in_quiet(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def next_run_time(settings: dict | None = None) -> datetime | None:
    """Local time of the next scheduled hunt, or None when the schedule is off."""
    sched = (settings or db.get_settings())["schedule"]
    if not sched.get("enabled"):
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT finished_at FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    now = datetime.now().astimezone()
    if row:
        due = datetime.fromisoformat(row["finished_at"]).astimezone() + timedelta(
            hours=max(0.25, float(sched.get("interval_hours", 4))))
        due = max(due, now)
    else:
        due = now
    start, end = int(sched.get("quiet_start", 23)), int(sched.get("quiet_end", 7))
    if _in_quiet(due.hour, start, end):
        wake = due.replace(hour=end, minute=0, second=0, microsecond=0)
        due = wake if wake > due else wake + timedelta(days=1)
    return due
