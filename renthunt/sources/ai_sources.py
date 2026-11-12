"""Pages you ask the app to watch, read with the local model."""
from __future__ import annotations

import hashlib
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import ai, db
from .base import Fetcher, Log, assign_town

MAX_CHUNKS = 6


def page_to_text(html: str, base_url: str) -> str:
    """Visible text with link targets kept, so the model can report unit URLs."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "head"]):
        tag.decompose()
    for link in soup.find_all("a", href=True):
        href = urljoin(base_url, link["href"])
        if href.startswith("http"):
            link.append(f" [{href}]")
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def split_text(text: str, size: int) -> list[str]:
    """Split on line boundaries into pieces of at most `size` characters."""
    chunks, current, length = [], [], 0
    for line in text.split("\n"):
        line = line[:size]
        if length + len(line) + 1 > size and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def to_item(prefix: str, raw: dict, settings: dict, page_url: str) -> dict | None:
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    town = assign_town(settings["towns"], f"{raw.get('town') or ''} {raw.get('address') or ''}")
    url = raw.get("url") if (raw.get("url") or "").startswith("http") else page_url
    return {
        "source": "watch",
        "source_id": prefix + _digest(raw.get("url") or "", title, raw.get("address") or "",
                                      str(raw.get("beds"))),
        "url": url,
        "title": title[:200],
        "town": town["name"] if town else (raw.get("town") or None),
        "state": town.get("state", "MA") if town else None,
        "address": raw.get("address"),
        "units": [{"beds": raw.get("beds"), "price_low": raw.get("price"),
                   "price_high": None, "sqft": raw.get("sqft")}],
        "baths": raw.get("baths"),
        "sqft": raw.get("sqft"),
        "description": raw.get("description") or "",
        "contact": raw.get("contact"),
        "available": raw.get("available"),
    }


def fetch_watch(settings: dict, fetcher: Fetcher, log: Log) -> tuple[list[dict], list[str]]:
    """Returns (items, keep_prefixes).

    A page whose text has not changed since the last successful read is not sent to
    the model again; its source-id prefix is returned so its listings stay alive.
    The same goes for a page that failed to load.
    """
    with db.connect() as conn:
        watches = [dict(r) for r in conn.execute("SELECT * FROM watch_urls WHERE enabled=1")]
    items, keep = [], []
    for watch in watches:
        prefix = f"w{watch['id']}-"
        name = watch["label"] or watch["url"]
        status, new_hash = "ok", watch["last_hash"]
        try:
            resp = fetcher.get(watch["url"])
            resp.raise_for_status()
            text = page_to_text(resp.text, str(resp.url))
            page_hash = hashlib.sha1(text.encode()).hexdigest()
            if page_hash == watch["last_hash"]:
                keep.append(prefix)
                status = "unchanged since last read"
            elif len(text) < 200:
                keep.append(prefix)
                status = "almost no readable text (the page may need JavaScript)"
                log(f"watch: {name}: {status}")
            else:
                chunks = split_text(text, ai.chunk_chars(settings))
                if len(chunks) > MAX_CHUNKS:
                    log(f"watch: {name} is very long; read the first {MAX_CHUNKS} of {len(chunks)} parts")
                    chunks = chunks[:MAX_CHUNKS]
                found = []
                for chunk in chunks:
                    found += ai.extract_listings(settings, watch["url"], chunk)
                page_items = {}
                for listing in found:
                    item = to_item(prefix, listing.model_dump(), settings, watch["url"])
                    if item:
                        page_items[item["source_id"]] = item
                items += page_items.values()
                new_hash = page_hash
                status = f"{len(page_items)} listing(s) found"
                log(f"watch: {name}: {status}")
        except Exception as exc:
            status = f"error: {ai.describe_error(exc)}"
            keep.append(prefix)
            log(f"watch: {name} failed: {ai.describe_error(exc)}")
        with db.connect() as conn:
            conn.execute(
                "UPDATE watch_urls SET last_hash=?, last_checked=?, last_status=? WHERE id=?",
                (new_hash, db.now(), status, watch["id"]),
            )
    return items, keep
