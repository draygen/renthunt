"""Listing alerts that arrive by email.

Zillow, Apartments.com, HotPads, Realtor.com and the like refuse automated visitors, but
they will email you the results of a saved search. This source reads those emails from a
mailbox over IMAP and has the local model pull the listings out of each one, the same way
it reads a watched page.

- The mailbox is opened read-only: nothing is marked read, moved or deleted.
- Only messages from the senders listed in settings are looked at, and each is read once.
- Two ways in. A Google read-only token file (`gmail_token_file` in settings; the one the
  career-agent project already keeps works as it is) reads Gmail through Google's API and
  cannot change anything even in principle. Otherwise IMAP, with the address and password
  from the environment (RENTHUNT_MAIL_USER, RENTHUNT_MAIL_PASSWORD, normally in .env; for
  Gmail an app password). Neither secret is ever copied into stored settings, and the
  token file is only read, never rewritten.
- An emailed listing cannot be re-checked, so it stays until no alert has mentioned it for
  `keep_days`, then drops off.
"""
from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
from bs4 import BeautifulSoup

from .. import ai, db
from .base import Fetcher, Log, SourceResult, assign_town

USER_ENV, PASSWORD_ENV = "RENTHUNT_MAIL_USER", "RENTHUNT_MAIL_PASSWORD"
SEEN_STATE = "mail_seen"   # {message key: date first read}; pruned once older than the search window
SCOPE = "mail"
MAX_CHUNKS = 4
LINK = "https://link.invalid/{}"   # short stand-ins for tracking links, swapped back afterwards
LINK_RE = re.compile(r"https://link\.invalid/(\d+)")


def credentials() -> tuple[str, str]:
    return os.environ.get(USER_ENV, "").strip(), os.environ.get(PASSWORD_ENV, "").strip()


def token_file(settings: dict) -> Path | None:
    path = (settings["sources"]["mail"].get("gmail_token_file") or "").strip()
    return Path(path).expanduser() if path else None


def status(settings: dict) -> dict:
    user, password = credentials()
    token = token_file(settings)
    last = db.get_state("mail_last") or {}
    return {"ready": bool(token and token.exists()) or bool(user and password),
            "via": "Google read-only token" if token else "IMAP",
            "token_missing": bool(token and not token.exists()),
            "user_env": USER_ENV, "password_env": PASSWORD_ENV,
            "last_checked": last.get("at"), "last_note": last.get("note")}


# ---------- mailboxes ----------
# A mailbox lists candidate messages as (key, sender, load) newest first, where load()
# returns the raw message bytes, and can be closed.

class ImapBox:
    def __init__(self, cfg: dict, connect: Callable | None = None):
        user, password = credentials()
        if not (user and password):
            raise RuntimeError(f"no mailbox sign-in. Set {USER_ENV} and {PASSWORD_ENV}, or gmail_token_file "
                               "(see README: Email alerts), or switch email alerts off.")
        self.box = connect() if connect else imaplib.IMAP4_SSL(
            cfg.get("host") or "imap.gmail.com", int(cfg.get("port", 993)), timeout=30)
        self.box.login(user, password)
        folder = cfg.get("folder") or "INBOX"
        if self.box.select(folder, readonly=True)[0] != "OK":
            raise RuntimeError(f"mail folder {folder!r} could not be opened")

    def _fetch(self, uid: bytes, what: str) -> bytes | None:
        ok, parts = self.box.uid("fetch", uid, what)
        return next((p[1] for p in parts or [] if isinstance(p, tuple)), None) if ok == "OK" else None

    def candidates(self, since: datetime, senders: list[str]):
        ok, data = self.box.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
        for uid in reversed(data[0].split() if ok == "OK" and data and data[0] else []):
            head = email.message_from_bytes(self._fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])") or b"",
                                            policy=email.policy.default)
            key = str(head.get("Message-ID") or "").strip() or f"uid:{uid.decode()}"
            yield key, str(head.get("From", "")), (lambda uid=uid: self._fetch(uid, "(BODY.PEEK[])"))

    def close(self) -> None:
        try:
            self.box.logout()
        except Exception:
            pass


class GmailBox:
    """Gmail through Google's API with a read-only OAuth token file."""
    API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

    def __init__(self, path: Path, transport: httpx.BaseTransport | None = None):
        try:
            grant = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"the Google token file {path} could not be read") from exc
        self.client = httpx.Client(timeout=30, transport=transport)
        try:
            resp = self.client.post(grant.get("token_uri") or "https://oauth2.googleapis.com/token", data={
                "grant_type": "refresh_token", "refresh_token": grant.get("refresh_token", ""),
                "client_id": grant.get("client_id", ""), "client_secret": grant.get("client_secret", "")})
        except httpx.HTTPError as exc:
            self.client.close()
            raise RuntimeError("could not reach Google to sign in to Gmail") from exc
        if resp.status_code != 200:
            self.client.close()
            try:
                why = resp.json().get("error") or resp.json().get("error_description")
            except ValueError:
                why = f"HTTP {resp.status_code}"
            raise RuntimeError(f"Google refused the stored Gmail token ({why}). It has probably expired or been "
                               "revoked; authorize again where the token was made, then retry.")
        self.client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def _get(self, url: str, **params) -> dict:
        resp = self.client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def candidates(self, since: datetime, senders: list[str]):
        names = " OR ".join(s.strip().lstrip("@") for s in senders if s.strip())
        if not names:
            return
        found = self._get(self.API, q=f"from:({names}) after:{int(since.timestamp())}", maxResults=100)
        for entry in found.get("messages") or []:  # newest first
            meta = self._get(f"{self.API}/{entry['id']}", format="metadata", metadataHeaders="From")
            sender = next((h["value"] for h in meta.get("payload", {}).get("headers", [])
                           if h.get("name", "").lower() == "from"), "")
            yield f"gmail:{entry['id']}", sender, (lambda mid=entry["id"]: base64.urlsafe_b64decode(
                self._get(f"{self.API}/{mid}", format="raw")["raw"] + "=="))

    def close(self) -> None:
        self.client.close()


def _sender_ok(sender: str, allowed: list[str]) -> bool:
    address = email.utils.parseaddr(sender)[1].lower()
    domain = address.rsplit("@", 1)[-1]
    return any(domain == a or domain.endswith("." + a) or address == a
               for a in (s.strip().lower().lstrip("@") for s in allowed) if a)


def message_text(message) -> tuple[str, list[str]]:
    """(readable text, links). Links appear in the text as short numbered stand-ins."""
    part = message.get_body(preferencelist=("html", "plain"))
    if part is None:
        return "", []
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() != "text/html":
        links: list[str] = []

        def swap(found: re.Match) -> str:
            links.append(found.group(0))
            return f"[{LINK.format(len(links) - 1)}]"
        return re.sub(r"https?://[^\s<>\"')\]]+", swap, content), links
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    links, index = [], {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.startswith("http"):
            continue
        if href not in index:
            index[href] = len(links)
            links.append(href)
        if anchor.get_text(strip=True):
            anchor.append(f" [{LINK.format(index[href])}]")
    lines = (" ".join(line.split()) for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line), links


def _item(raw: dict, links: list[str], sender_domain: str, settings: dict) -> dict | None:
    title = (raw.get("title") or "").strip()
    address = (raw.get("address") or "").strip()
    stated = (raw.get("town") or "").strip()
    town = assign_town(settings["towns"], stated or address or title)
    if not title or not town:
        return None  # an alert for somewhere that is not on your town list
    state = re.search(r"\b([A-Z]{2})\s+\d{5}\b", address)
    if state and town.get("state") and state.group(1) != town["state"]:
        return None
    found = LINK_RE.search(raw.get("url") or "")
    url = links[int(found.group(1))] if found and int(found.group(1)) < len(links) else None
    key = "|".join([sender_domain, re.sub(r"\W+", " ", (address or title).lower()).strip(), str(raw.get("beds"))])
    return {
        "source": "mail",
        "source_id": hashlib.sha1(key.encode()).hexdigest()[:16],
        "scope": SCOPE,
        "url": url or f"https://www.google.com/search?q={re.sub(r'[^A-Za-z0-9]+', '+', address or title)}",
        "title": title[:200],
        "town": town["name"], "state": town.get("state", "MA"),
        "address": address or None,
        "units": [{"beds": raw.get("beds"), "price_low": raw.get("price"), "price_high": None,
                   "sqft": raw.get("sqft")}],
        "baths": raw.get("baths"), "sqft": raw.get("sqft"),
        "description": "\n".join(p for p in (raw.get("description") or "", f"From a {sender_domain} email alert.") if p),
        "contact": raw.get("contact"),
        "available": raw.get("available"),
    }


def read_message(message, settings: dict) -> list[dict]:
    """Listings in one alert email that are in your towns."""
    sender_domain = email.utils.parseaddr(message.get("From", ""))[1].rsplit("@", 1)[-1].lower()
    sender_domain = ".".join(sender_domain.split(".")[-2:]) or "email"
    text, links = message_text(message)
    if len(text) < 80:
        return []
    size = ai.chunk_chars(settings)
    chunks = [text[i:i + size] for i in range(0, len(text), size)][:MAX_CHUNKS]
    label = f"email from {sender_domain}: {str(message.get('Subject', ''))[:120]}"
    items = {}
    for chunk in chunks:
        for listing in ai.extract_listings(settings, label, chunk):
            item = _item(listing.model_dump(), links, sender_domain, settings)
            if item:
                items[item["source_id"]] = item
    return list(items.values())


def _expire(keep_days: float, now: datetime) -> int:
    cutoff = (now - timedelta(days=keep_days)).isoformat()
    with db.connect() as conn:
        return conn.execute("UPDATE listings SET active=0 WHERE source='mail' AND active=1 AND last_seen<?",
                            (cutoff,)).rowcount


def fetch(settings: dict, fetcher: Fetcher | None, log: Log,
          box=None, now: datetime | None = None, store: bool = True) -> SourceResult:
    """Read new alert emails. With store=False nothing is remembered or expired (dry run)."""
    cfg = settings["sources"]["mail"]
    now = now or datetime.now(timezone.utc)
    result = SourceResult(labels={SCOPE: "email alerts"})
    # Emailed listings are never "missing" just because this hunt's mail did not mention them.
    result.skipped[SCOPE] = "kept until no alert mentions it"
    since = now - timedelta(days=float(cfg.get("days_back", 7)))
    seen: dict = db.get_state(SEEN_STATE) or {}
    limit = max(1, int(cfg.get("max_messages_per_hunt", 15)))
    senders = list(cfg.get("senders") or [])

    if box is None:
        token = token_file(settings)
        box = GmailBox(token) if token else ImapBox(cfg)
    read = skipped_old = troubles = 0
    try:
        fresh = []
        for key, sender, load in box.candidates(since, senders):
            if not _sender_ok(sender, senders):
                continue
            if key in seen:
                skipped_old += 1
                continue
            fresh.append((key, load))
        if fresh and not ai.status(settings, fresh=True)["ok"]:
            raise RuntimeError("the local model is not reachable, so the alert emails could not be read")
        for key, load in fresh[:limit]:
            try:
                body = load()
            except Exception:
                body = None
            if body is None:
                troubles += 1
                continue
            message = email.message_from_bytes(body, policy=email.policy.default)
            try:
                items = read_message(message, settings)
            except Exception as exc:
                troubles += 1
                log(f"mail: could not read \"{str(message.get('Subject', ''))[:60]}\": {ai.describe_error(exc)}")
                continue  # not remembered, so it is tried again next hunt
            result.items += items
            seen[key] = now.isoformat()
            read += 1
            log(f"mail: \"{str(message.get('Subject', ''))[:60]}\": {len(items)} listing(s) in your towns")
        waiting = max(0, len(fresh) - limit)
    finally:
        box.close()

    result.note = (f"{read} new alert email(s) read" + (f", {waiting} left for the next hunt" if waiting else "")
                   + (f", {troubles} could not be read" if troubles else ""))
    if troubles:
        result.failed["mail:unread"] = f"{troubles} alert email(s) could not be read"
        result.labels["mail:unread"] = "email alerts"
    if store:
        cutoff = (since - timedelta(days=2)).isoformat()
        db.set_state(SEEN_STATE, {k: v for k, v in seen.items() if v >= cutoff})
        db.set_state("mail_last", {"at": now.isoformat(), "note": result.note})
        expired = _expire(float(cfg.get("keep_days", 21)), now)
        if expired:
            log(f"mail: {expired} emailed listing(s) not mentioned for {cfg.get('keep_days', 21)} days dropped")
    log(f"mail: {result.note}" + (f" ({skipped_old} already read)" if skipped_old else ""))
    return result


def main(argv: list[str] | None = None) -> int:
    """Dry run: read the alert emails and print what would be found. Stores nothing."""
    import json

    db.init()
    settings = db.get_settings()
    try:
        result = fetch(settings, None, print, store=False)
    except Exception as exc:
        print(f"failed: {exc}")
        return 1
    for item in result.items:
        unit = item["units"][0]
        print(json.dumps({"title": item["title"], "town": item["town"], "address": item["address"],
                          "rent": unit["price_low"], "beds": unit["beds"], "url": item["url"][:90]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
