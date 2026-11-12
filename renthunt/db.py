"""SQLite storage: schema, settings, and listing helpers."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    town TEXT, state TEXT, address TEXT,
    lat REAL, lng REAL,
    price INTEGER,            -- lowest advertised rent
    price_max INTEGER,
    beds_min REAL, beds_max REAL,
    baths REAL, sqft INTEGER,
    units TEXT,               -- JSON [{beds, price_low, price_high, sqft, available}]
    description TEXT,
    images TEXT,              -- JSON [url]
    amenities TEXT,           -- JSON [str]
    contact TEXT,
    posted_at TEXT, available TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    missed_runs INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    detail_fetched INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT, dup_of INTEGER,
    price_prev INTEGER,       -- previous price when it last changed
    score INTEGER NOT NULL DEFAULT 0,
    match_price INTEGER,      -- cheapest rent among units that fit the bedroom criteria
    is_match INTEGER NOT NULL DEFAULT 0,
    reasons TEXT, flags TEXT, -- JSON [str]
    status TEXT NOT NULL DEFAULT 'new',
    favorite INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_match ON listings(is_match, active, hidden);
CREATE INDEX IF NOT EXISTS idx_listings_fp ON listings(fingerprint);
CREATE TABLE IF NOT EXISTS price_history (
    listing_id INTEGER NOT NULL, price INTEGER NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL, finished_at TEXT,
    trigger TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
    summary TEXT, log TEXT
);
CREATE TABLE IF NOT EXISTS watch_urls (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE, label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_hash TEXT, last_checked TEXT, last_status TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JSON_COLS = ("units", "images", "amenities", "reasons", "flags")
STATUSES = ("new", "seen", "contacted", "touring", "applied", "rejected")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------- settings ----------

def _merge(defaults, stored):
    """Stored values win; keys missing from stored fall back to defaults."""
    if isinstance(defaults, dict) and isinstance(stored, dict):
        out = {k: _merge(v, stored[k]) if k in stored else copy.deepcopy(v) for k, v in defaults.items()}
        out.update({k: v for k, v in stored.items() if k not in defaults})
        return out
    return stored


def get_settings() -> dict:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='settings'").fetchone()
    stored = json.loads(row["value"]) if row else {}
    return _merge(config.DEFAULT_SETTINGS, stored)


def save_settings(settings: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('settings', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(settings),),
        )


def get_state(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (f"state:{key}",)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"state:{key}", json.dumps(value)),
        )


# ---------- listings ----------

def row_to_listing(row: sqlite3.Row) -> dict:
    d = dict(row)
    for col in JSON_COLS:
        d[col] = json.loads(d[col]) if d.get(col) else []
    for col in ("active", "is_match", "favorite", "hidden", "detail_fetched", "alerted"):
        d[col] = bool(d[col])
    return d


def get_listing(listing_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not row:
            return None
        listing = row_to_listing(row)
        listing["price_history"] = [
            dict(r) for r in conn.execute(
                "SELECT price, at FROM price_history WHERE listing_id=? ORDER BY at", (listing_id,)
            )
        ]
    return listing


SORTS = {
    "score": "score DESC, first_seen DESC",
    "newest": "first_seen DESC, score DESC",
    "price_asc": "COALESCE(match_price, price) ASC NULLS LAST",
    "price_desc": "COALESCE(match_price, price) DESC NULLS LAST",
}


def query_listings(
    *, view: str = "matches", towns: list[str] | None = None, sources: list[str] | None = None,
    min_price: int | None = None, max_price: int | None = None,
    min_beds: float | None = None, max_beds: float | None = None,
    text: str | None = None, new_since_hours: float | None = None, search_notes: bool = True,
    sort: str = "score", limit: int = 200, offset: int = 0,
) -> tuple[list[dict], int]:
    where, args = [], []
    if view == "matches":
        where.append("is_match=1 AND active=1 AND hidden=0 AND dup_of IS NULL AND status!='rejected'")
    elif view == "new":
        where.append("is_match=1 AND active=1 AND hidden=0 AND dup_of IS NULL AND status='new'")
    elif view == "favorites":
        where.append("favorite=1")
    elif view == "pipeline":
        where.append("status IN ('contacted','touring','applied')")
    elif view == "hidden":
        where.append("(hidden=1 OR status='rejected')")
    elif view == "all":
        where.append("dup_of IS NULL")
    if towns:
        where.append(f"town IN ({','.join('?' * len(towns))})")
        args += towns
    if sources:
        where.append(f"source IN ({','.join('?' * len(sources))})")
        args += sources
    price_col = "COALESCE(match_price, price)"
    if min_price is not None:
        where.append(f"{price_col} >= ?")
        args.append(min_price)
    if max_price is not None:
        where.append(f"{price_col} <= ?")
        args.append(max_price)
    if min_beds is not None:
        where.append("beds_max >= ?")
        args.append(min_beds)
    if max_beds is not None:
        where.append("beds_min <= ?")
        args.append(max_beds)
    if text:
        like = f"%{text}%"
        fields = ["title", "description", "address"] + (["notes"] if search_notes else [])
        where.append("(" + " OR ".join(f"{f} LIKE ?" for f in fields) + ")")
        args += [like] * len(fields)
    if new_since_hours is not None:
        where.append("first_seen >= ?")
        args.append((datetime.now(timezone.utc) - timedelta(hours=float(new_since_hours)))
                    .isoformat(timespec="seconds"))
    clause = " AND ".join(where) or "1"
    order = SORTS.get(sort, SORTS["score"])
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM listings WHERE {clause}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM listings WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
    return [row_to_listing(r) for r in rows], total


def get_listings_by_ids(ids: list[int]) -> list[dict]:
    """Listings in the order the ids were given; unknown ids are skipped."""
    if not ids:
        return []
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM listings WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    found = {r["id"]: row_to_listing(r) for r in rows}
    return [found[i] for i in ids if i in found]


def update_listing(listing_id: int, **fields) -> dict | None:
    allowed = {"status", "favorite", "hidden", "notes"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    for key in ("favorite", "hidden"):
        if key in fields:
            fields[key] = int(bool(fields[key]))
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        with connect() as conn:
            conn.execute(f"UPDATE listings SET {sets} WHERE id=?", [*fields.values(), listing_id])
    return get_listing(listing_id)


def counts() -> dict:
    base = "active=1 AND hidden=0 AND dup_of IS NULL AND status!='rejected'"
    with connect() as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "matches": one(f"SELECT COUNT(*) FROM listings WHERE is_match=1 AND {base}"),
            "new": one(f"SELECT COUNT(*) FROM listings WHERE is_match=1 AND status='new' AND {base}"),
            "favorites": one("SELECT COUNT(*) FROM listings WHERE favorite=1"),
            "pipeline": one("SELECT COUNT(*) FROM listings WHERE status IN ('contacted','touring','applied')"),
            "hidden": one("SELECT COUNT(*) FROM listings WHERE hidden=1 OR status='rejected'"),
            "all": one("SELECT COUNT(*) FROM listings WHERE dup_of IS NULL"),
        }
