"""Pages you ask the app to watch: property-manager vacancy lists and the like.

Each page is fetched with a plain HTTP request, or in a headless browser when the page is
set to need one. Listings the page states in a structured form (an AppFolio listing page,
RentPress community data, schema.org JSON-LD) are read directly; otherwise the local model reads the page text.

Every read ends in one of these states, stored per page:

    ok          read completely, listings found
    empty       read completely, and the page lists no vacancies
    partial     read, but the page was too long to read in full
    blocked     the site refused the request or showed a bot check
    incomplete  the page came back without its content (it probably needs the browser)
    failed      the page or the extraction failed

Only `ok` and `empty` count as a successful check. Anything else keeps the page's existing
listings (marked unverified) and is never remembered as "unchanged".
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .. import ai, db
from . import render, structured
from .base import Fetcher, Log, SourceResult, assign_town

MAX_CHUNKS = 6
GOOD_STATES = ("ok", "empty")

BLOCKED_STATUS = (401, 403, 407, 429, 451)
BOT_CHECK = ("just a moment", "checking your browser", "verify you are human", "verifying you are human",
             "are you a robot", "captcha", "access denied", "unusual traffic", "pardon our interruption",
             "request unsuccessful", "attention required", "enable cookies")
NEEDS_SCRIPT = ("enable javascript", "requires javascript", "javascript is required",
                "javascript is disabled", "turn on javascript", "need to enable javascript")
NO_VACANCY = ("no vacancies", "no vacancy", "no units available", "no apartments available",
              "no rentals available", "nothing available", "no availability", "no current listings",
              "no listings", "no properties available", "no available units", "no available apartments",
              "currently full", "fully occupied", "fully leased", "no results", "check back soon")
RENT_HINT = re.compile(r"\$\s?\d{1,2},?\d{3}\b|\$\s?[4-9]\d{2}\b")


@dataclass
class PageRead:
    state: str
    status: str                       # one plain sentence for the settings screen and the log
    items: list[dict] = field(default_factory=list)
    page_hash: str | None = None      # set only when this read may be remembered
    method: str | None = None         # how the listings were read
    unchanged: bool = False


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


def _page_hash(html: str, text: str) -> str:
    """Fingerprint of everything listings are read from: visible text, JSON-LD, RentPress data."""
    blocks = re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I)
    blocks.append(structured.data_properties(html) or "")
    return hashlib.sha1("\n".join([text, *blocks]).encode()).hexdigest()


def blocked_reason(status_code: int, text: str, html: str = "") -> str | None:
    """Why the site turned us away, or None when it did not."""
    if status_code in BLOCKED_STATUS:
        return f"the site refused the request (HTTP {status_code})"
    if len(html) < 6000 and re.search(r"<title>\s*(client challenge|just a moment)", html, re.I):
        return "the site showed a bot check instead of the page"  # Zumper, PadMapper and the like
    low = text[:3000].lower()
    if len(text) < 3000 and any(mark in low for mark in BOT_CHECK):
        return "the site showed a bot check instead of the page"
    return None


def to_item(prefix: str, raw: dict, settings: dict, page_url: str) -> dict | None:
    """A raw listing as a RentHunt item. None when it has no title or is outside your towns."""
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    address = raw.get("address") or ""
    stated = (raw.get("town") or "").strip()
    town = assign_town(settings["towns"], stated or address)
    if stated and not town:
        return None  # a named town that is not on your list
    state = re.search(r"\b([A-Z]{2})\s+\d{5}\b", address)
    if town and state and town.get("state") and state.group(1) != town["state"]:
        return None  # same town name, different state
    url = raw.get("url") if (raw.get("url") or "").startswith("http") else page_url
    description = raw.get("description") or ""
    if raw.get("headline") and raw["headline"] not in description:
        description = f"{raw['headline']}\n{description}".strip()
    return {
        "source": "watch",
        "source_id": prefix + "-" + _digest(raw.get("url") or "", title, address, str(raw.get("beds"))),
        "scope": prefix,
        "url": url,
        "title": title[:200],
        "town": town["name"] if town else None,
        "state": town.get("state", "MA") if town else None,
        "address": address or None,
        "lat": raw.get("lat"), "lng": raw.get("lng"),
        "units": raw.get("units") or [{"beds": raw.get("beds"), "price_low": raw.get("price"),
                                       "price_high": None, "sqft": raw.get("sqft")}],
        "baths": raw.get("baths"),
        "sqft": raw.get("sqft"),
        "description": description,
        "images": raw.get("images") or [],
        "amenities": raw.get("amenities") or [],
        "contact": raw.get("contact"),
        "available": raw.get("available"),
    }


def _fetch_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "could not connect to the site"
    if isinstance(exc, httpx.TimeoutException):
        return "the site took too long to answer"
    if isinstance(exc, httpx.HTTPError):
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    return str(exc)[:200] or type(exc).__name__


def _load(watch: dict, fetcher: Fetcher, browser: render.Browser | None) -> tuple[int, str, str]:
    """(status code, final URL, HTML) by HTTP, or through the browser when the page asks for it."""
    if watch.get("render"):
        if browser is None:
            raise render.RenderUnavailable("browser rendering is not available in this run")
        return browser.fetch(watch["url"])
    resp = fetcher.get(watch["url"])
    return resp.status_code, str(resp.url), resp.text


def read_page(watch: dict, settings: dict, fetcher: Fetcher, browser: render.Browser | None = None,
              model_ok: Callable[[], bool] | None = None, log: Log = lambda _: None) -> PageRead:
    """Read one watched page. Touches no stored data, so it is safe for a dry run."""
    prefix = f"w{watch['id']}"
    rendered = bool(watch.get("render"))
    try:
        status_code, final_url, html = _load(watch, fetcher, browser)
    except render.RenderUnavailable as exc:
        return PageRead("failed", str(exc))
    except render.RenderIncomplete as exc:
        return PageRead("incomplete", str(exc))
    except Exception as exc:
        return PageRead("failed", f"could not load the page: {_fetch_error(exc)}")

    text = page_to_text(html, final_url)
    blocked = blocked_reason(status_code, text, html)
    if blocked:
        return PageRead("blocked", blocked)
    if status_code >= 400:
        return PageRead("failed", f"the site answered HTTP {status_code}")

    page_hash = _page_hash(html, text)
    if watch.get("last_hash") == page_hash and watch.get("state") in (*GOOD_STATES, "partial"):
        return PageRead(watch["state"], "unchanged since the last good read", page_hash=page_hash,
                        method=watch.get("method"), unchanged=True)

    def finish(raws: list[dict], method: str, partial: bool = False) -> PageRead:
        items = {}
        for raw in raws:
            item = to_item(prefix, raw, settings, watch["url"])
            if item:
                items[item["source_id"]] = item
        elsewhere = sum(1 for raw in raws if to_item(prefix, raw, settings, watch["url"]) is None)
        if items:
            status = f"{len(items)} listing(s) found" + (f" ({elsewhere} more outside your towns)" if elsewhere else "")
        elif raws:
            status = f"no vacancies in your towns ({elsewhere} listed elsewhere)"
        else:
            status = "no vacancies listed"
        status += f", read from {method}"
        if partial:
            status += "; the page is too long to read in full"
        state = "partial" if partial else "ok" if items else "empty"
        return PageRead(state, status, list(items.values()), page_hash, method)

    found = structured.extract(html, final_url)
    if found is not None:
        return finish(*found)

    low = text.lower()
    if len(text) < 200 or (len(text) < 1500 and any(mark in low for mark in NEEDS_SCRIPT)):
        hint = ("the browser did not get the page's content either" if rendered
                else "the page came back almost empty; it probably needs browser rendering")
        return PageRead("incomplete", hint)

    if not (model_ok() if model_ok else ai.status(settings, fresh=True)["ok"]):
        return PageRead("failed", "this page has no structured listings and the local model is not reachable")
    chunks = split_text(text, ai.chunk_chars(settings))
    partial = len(chunks) > MAX_CHUNKS
    if partial:
        log(f"watch: {watch['label'] or watch['url']} is very long; read the first {MAX_CHUNKS} of {len(chunks)} parts")
        chunks = chunks[:MAX_CHUNKS]
    raws: list[dict] = []
    try:
        for chunk in chunks:
            raws += [listing.model_dump() for listing in ai.extract_listings(settings, watch["url"], chunk)]
    except Exception as exc:
        return PageRead("failed", f"the model could not read the page: {ai.describe_error(exc)}")
    if partial:
        return finish(raws, "the page text by the local model", partial=True)
    if not raws and RENT_HINT.search(text) and not any(phrase in low for phrase in NO_VACANCY):
        # Rents are on the page, yet nothing came out: more likely a misread than an empty list.
        return PageRead("failed", "the page mentions rents but no vacancy could be read from it")
    return finish(raws, "the page text by the local model")


def fetch_watch(settings: dict, fetcher: Fetcher, log: Log, browser: render.Browser | None = None) -> SourceResult:
    """Read every enabled watched page and record how each read went."""
    with db.connect() as conn:
        watches = [dict(r) for r in conn.execute("SELECT * FROM watch_urls ORDER BY id")]
    result = SourceResult()
    model_ok = lru_cache(maxsize=1)(lambda: ai.status(settings, fresh=True)["ok"])  # asked once, if at all
    for watch in watches:
        scope = f"w{watch['id']}"
        name = watch["label"] or watch["url"]
        result.labels[scope] = name
        if not watch["enabled"]:
            result.skipped[scope] = "switched off"
            continue
        try:
            page = read_page(watch, settings, fetcher, browser, model_ok, log)
        except Exception as exc:  # a surprise in one page must not sink the others
            page = PageRead("failed", f"{type(exc).__name__}: {str(exc)[:160]}")
        now = db.now()
        good = page.state in GOOD_STATES
        if page.unchanged:
            result.unchanged.add(scope)
        elif good:
            result.checked.add(scope)
            result.items += page.items
        else:
            result.items += page.items  # a partial read still found real listings
            result.failed[scope] = page.status
        log(f"watch: {name}: {page.status}" if good or page.unchanged else f"watch: {name}: NOT CHECKED ({page.state}): {page.status}")
        with db.connect() as conn:
            if page.unchanged:
                conn.execute("UPDATE watch_urls SET last_checked=?, last_success=?, last_status=? WHERE id=?",
                             (now, now if good else watch.get("last_success"), page.status, watch["id"]))
            elif page.page_hash:  # ok, empty or partial: this read may be remembered
                conn.execute(
                    "UPDATE watch_urls SET last_hash=?, last_checked=?, last_status=?, state=?, method=?, "
                    "last_count=?, last_success=? WHERE id=?",
                    (page.page_hash, now, page.status, page.state, page.method, len(page.items),
                     now if good else watch.get("last_success"), watch["id"]))
            else:  # not a successful read: keep the last good hash and time, never cache this one
                conn.execute("UPDATE watch_urls SET last_checked=?, last_status=?, state=? WHERE id=?",
                             (now, page.status, page.state, watch["id"]))
    return result


def main(argv: list[str] | None = None) -> int:
    """Dry run: read one page and print what would be found. Stores nothing."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="renthunt check-page", description=main.__doc__)
    parser.add_argument("url")
    parser.add_argument("--browser", action="store_true", help="render the page in the headless browser")
    args = parser.parse_args(argv)
    db.init()
    settings = db.get_settings()
    fetcher = Fetcher(settings.get("request_delay_seconds", 2.5), print)
    browser = render.Browser() if args.browser else None
    try:
        page = read_page({"id": 0, "url": args.url, "label": "", "render": args.browser}, settings,
                         fetcher, browser, log=print)
    finally:
        fetcher.close()
        if browser:
            browser.close()
    print(f"state:  {page.state}\nstatus: {page.status}")
    for item in page.items:
        units = item["units"]
        print(json.dumps({"title": item["title"], "town": item["town"], "address": item["address"],
                          "rent": [u["price_low"] for u in units] if len(units) > 1 else units[0]["price_low"],
                          "beds": [u["beds"] for u in units] if len(units) > 1 else units[0]["beds"],
                          "baths": item["baths"],
                          "available": item["available"], "url": item["url"]}))
    return 0 if page.state in GOOD_STATES else 1


if __name__ == "__main__":
    raise SystemExit(main())
