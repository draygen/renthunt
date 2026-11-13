"""Missing-listing handling, run status, and database upgrades."""
import sqlite3
import uuid
from pathlib import Path
from unittest import mock

import httpx

from renthunt import config, db, hunt
from renthunt.sources import craigslist, rentcom
from renthunt.sources.base import Fetcher, SourceResult

from . import DbTest

CL_PAGE = """<html><body><ol>
<li class="cl-static-search-result"><a href="https://boston.craigslist.org/nwb/apa/d/x/{id}.html">
<div class="title">{title}</div><div class="details"><div class="price">$1,800</div></div></a></li>
</ol><script type="application/ld+json">{{"itemListElement": [{{"item": {{"name": "{title}",
"numberOfBedrooms": 2, "address": {{"addressLocality": "{town}", "addressRegion": "MA"}}}}}}]}}</script></body></html>"""
EMPTY = "<html><body><ol></ol></body></html>"


class ScopedExpiryTest(DbTest):
    def setUp(self):
        super().setUp()
        for town in self.settings["towns"]:
            town["enabled"] = town["name"] in ("Lowell", "Dracut")
        self.settings["sources"]["rentcom"]["enabled"] = False
        self.settings["sources"]["craigslist"]["detail_fetch_limit"] = 0
        db.save_settings(self.settings)

    def hunt(self, pages: dict) -> dict:
        """pages: ZIP -> HTML, or an int status code for a failing search."""
        def handler(request):
            page = pages[request.url.params["postal"]]
            return httpx.Response(page, text="no") if isinstance(page, int) else httpx.Response(200, text=page)
        with db.connect() as conn:  # tests run hunts back to back; real ones are hours apart
            conn.execute("UPDATE listings SET last_seen='2026-01-01T00:00:00+00:00'")
        fetcher = lambda *a, **k: Fetcher(0, lambda _: None, transport=httpx.MockTransport(handler), min_delay=0)  # noqa: E731
        with mock.patch.object(hunt, "Fetcher", fetcher):
            return hunt.run("test")

    def by_town(self) -> dict:
        with db.connect() as conn:
            return {r["town"]: db.row_to_listing(r) for r in conn.execute("SELECT * FROM listings")}

    def test_failed_search_does_not_count_as_missing(self):
        lowell = CL_PAGE.format(id="111", title="Lowell two bed", town="Lowell")
        dracut = CL_PAGE.format(id="222", title="Dracut two bed", town="Dracut")
        first = self.hunt({"01852": lowell, "01826": dracut})
        self.assertEqual((first["status"], first["sources"]["craigslist"]["found"]), ("ok", 2))
        self.assertEqual({l["scope"] for l in self.by_town().values()}, {"town:Lowell", "town:Dracut"})

        for _ in range(4):  # Lowell's search is down; Dracut's runs and no longer shows its post
            run = self.hunt({"01852": 503, "01826": EMPTY})
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["sources"]["craigslist"]["unverified"], 1)
        self.assertTrue(run["sources"]["craigslist"]["problems"][0].startswith("Lowell: "))
        rows = self.by_town()
        self.assertEqual((rows["Lowell"]["active"], rows["Lowell"]["missed_runs"], rows["Lowell"]["unverified"]), (True, 0, True))
        self.assertEqual((rows["Dracut"]["active"], rows["Dracut"]["unverified"]), (False, False))  # expired as configured

        self.hunt({"01852": lowell, "01826": EMPTY})
        self.assertFalse(self.by_town()["Lowell"]["unverified"])

    def test_checked_and_absent_uses_the_expiry_setting(self):
        lowell = CL_PAGE.format(id="111", title="Lowell two bed", town="Lowell")
        self.hunt({"01852": lowell, "01826": EMPTY})
        for expected in (1, 2):
            self.hunt({"01852": EMPTY, "01826": EMPTY})
            row = self.by_town()["Lowell"]
            self.assertEqual((row["missed_runs"], row["active"]), (expected, True))
        self.hunt({"01852": EMPTY, "01826": EMPTY})
        self.assertFalse(self.by_town()["Lowell"]["active"])  # inactive_after_missed_runs = 3

    def test_every_search_failing_is_a_failed_hunt(self):
        kept = self.add_listing(scope="town:Lowell")
        run = self.hunt({"01852": 503, "01826": 403})
        self.assertEqual(run["status"], "failed")
        self.assertIn("error", run["sources"]["craigslist"])
        self.assertTrue(self.row(kept)["active"] and self.row(kept)["unverified"])

    def test_listing_stored_before_scopes_existed(self):
        old = self.add_listing()  # no scope: we cannot tell which search it came from
        self.hunt({"01852": 503, "01826": EMPTY})
        self.assertEqual((self.row(old)["missed_runs"], self.row(old)["unverified"]), (0, True))
        self.hunt({"01852": EMPTY, "01826": EMPTY})
        self.assertEqual((self.row(old)["missed_runs"], self.row(old)["unverified"]), (1, False))

    def test_source_that_crashes_leaves_its_listings_alone(self):
        kept = self.add_listing(scope="town:Lowell")
        with mock.patch.object(craigslist, "fetch", side_effect=ValueError("page layout changed")):
            run = hunt.run("test")
        self.assertEqual(run["status"], "failed")
        self.assertEqual((self.row(kept)["missed_runs"], self.row(kept)["unverified"]), (0, True))

    def test_rentcom_second_page_failing_makes_the_town_partial(self):
        self.settings["sources"]["rentcom"]["pages_per_town"] = 2
        item = {"source": "rentcom", "source_id": "r1", "url": "https://www.rent.com/x", "title": "Mill Lofts",
                "locality": "Lowell", "state": "MA", "units": [{"beds": 1.0, "price_low": 1700, "price_high": None, "sqft": None}]}
        calls = []

        def parse(html):
            calls.append(html)
            if html == "boom":
                raise ValueError("page layout changed: no embedded search data")
            return [dict(item)]

        def handler(request):
            lowell_page_2 = "lowell" in str(request.url) and request.url.params.get("page") == "2"
            return httpx.Response(200, text="boom" if lowell_page_2 else "ok")
        fetcher = Fetcher(0, lambda _: None, transport=httpx.MockTransport(handler), min_delay=0)
        with mock.patch.object(rentcom, "parse_search", parse):
            result = rentcom.fetch(self.settings, fetcher, lambda _: None)
        self.assertEqual(len(result.items), 1)                       # page 1 is kept
        self.assertIn("page 2", result.failed["town:Lowell"])        # but Lowell was not read in full
        self.assertEqual(result.checked, {"town:Dracut"})

    def test_plain_list_sources_still_work(self):
        self.add_listing(source="craigslist", source_id="gone", scope="*")
        simple = [{"source": "craigslist", "source_id": "s1", "url": "https://x.test/1", "title": "Simple",
                   "town": "Lowell", "state": "MA", "units": [{"beds": 2.0, "price_low": 1500, "price_high": None, "sqft": None}]}]
        with mock.patch.object(craigslist, "fetch", return_value=simple):
            run = hunt.run("test")
        self.assertEqual((run["status"], run["sources"]["craigslist"]["found"]), ("ok", 1))
        with db.connect() as conn:
            missed = dict(conn.execute("SELECT source_id, missed_runs FROM listings").fetchall())
        self.assertEqual(missed, {"gone": 1, "s1": 0})

    def test_skipped_scope_is_left_untouched(self):
        kept = self.add_listing(scope="town:Lowell")
        result = SourceResult(checked={"town:Dracut"}, skipped={"town:Lowell": "not due"})
        with db.connect() as conn:
            hunt._settle(conn, "craigslist", result, db.now(), 3)
        self.assertEqual((self.row(kept)["missed_runs"], self.row(kept)["unverified"]), (0, False))


OLD_SCHEMA = """
CREATE TABLE listings (id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, url TEXT NOT NULL,
    title TEXT NOT NULL, town TEXT, state TEXT, address TEXT, lat REAL, lng REAL, price INTEGER, price_max INTEGER,
    beds_min REAL, beds_max REAL, baths REAL, sqft INTEGER, units TEXT, description TEXT, images TEXT, amenities TEXT,
    contact TEXT, posted_at TEXT, available TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    missed_runs INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, detail_fetched INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT, dup_of INTEGER, price_prev INTEGER, score INTEGER NOT NULL DEFAULT 0, match_price INTEGER,
    is_match INTEGER NOT NULL DEFAULT 0, reasons TEXT, flags TEXT, status TEXT NOT NULL DEFAULT 'new',
    favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '', UNIQUE(source, source_id));
CREATE TABLE watch_urls (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, last_hash TEXT, last_checked TEXT, last_status TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO listings (source, source_id, url, title, town, units, first_seen, last_seen, favorite, notes, status)
    VALUES ('watch', 'w3-abc', 'https://x.test', '5 Pine St', 'Lowell', '[]', '2026-11-01', '2026-11-01', 1, 'mine', 'applied');
INSERT INTO watch_urls (id, url, label, last_hash, last_status) VALUES (3, 'https://x.test', 'PM', 'h', '1 listing(s) found');
INSERT INTO settings VALUES ('settings', '{"sources": {"craigslist": {"enabled": false, "radius_miles": 9}}, "criteria": {"max_price": 2100}}');
"""


class MigrationTest(DbTest):
    def test_database_from_the_first_release_upgrades_in_place(self):
        config.DB_PATH = Path(config.DATA_DIR) / f"old-{uuid.uuid4().hex}.db"
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(OLD_SCHEMA)
        conn.commit()
        conn.close()
        db.init()
        db.init()  # running it again changes nothing
        with db.connect() as conn:
            row = db.row_to_listing(conn.execute("SELECT * FROM listings").fetchone())
            watch = dict(conn.execute("SELECT * FROM watch_urls").fetchone())
        self.assertEqual((row["favorite"], row["notes"], row["status"]), (True, "mine", "applied"))
        self.assertEqual((row["scope"], row["unverified"]), ("w3", False))
        self.assertEqual((watch["render"], watch["state"], watch["last_hash"]), (0, None, "h"))
        settings = db.get_settings()
        self.assertEqual(settings["sources"]["craigslist"], {"enabled": False, "radius_miles": 9, "detail_fetch_limit": 25})
        self.assertEqual(settings["criteria"]["max_price"], 2100)
        self.assertFalse(settings["sources"]["rentcast"]["enabled"])  # new source arrives switched off
        hunt.rescore_all()
