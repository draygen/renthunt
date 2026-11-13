"""Watched pages: structured extraction, and telling an empty page from a failed read."""
from unittest import mock

import httpx

from renthunt import ai, db, hunt
from renthunt.sources import ai_sources, render, structured
from renthunt.sources.base import Fetcher

from . import FIXTURES, DbTest

PAGE = "https://pm.example.test/vacancies"
PROSE = ("<html><body><h1>Vacancies</h1>" + "<p>We manage homes across the Merrimack Valley and "
         "are happy to answer questions about any of them during office hours.</p>" * 4 + "{body}</body></html>")


def fetcher_for(handler) -> Fetcher:
    return Fetcher(0, lambda _: None, transport=httpx.MockTransport(handler), min_delay=0)


def serve(body: str, status: int = 200):
    return fetcher_for(lambda request: httpx.Response(status, text=body))


def model(listings: list[dict]):
    """Stand in for the local model: it is reachable and returns these listings."""
    found = [ai.ExtractedListing(**l) for l in listings]
    return mock.patch.multiple(ai, status=lambda *a, **k: {"ok": True},
                               extract_listings=lambda *a, **k: list(found))


class StructuredTest(DbTest):
    def test_appfolio_page_is_read_without_the_model(self):
        html = (FIXTURES / "appfolio_listings.html").read_text()
        raws, method = structured.extract(html, "https://x.appfolio.com/listings")
        self.assertEqual(method, "AppFolio listing page")
        self.assertEqual([r["price"] for r in raws], [1925, 2250, 1700])
        first = raws[0]
        self.assertEqual((first["beds"], first["baths"], first["sqft"]), (1.0, 1.0, 732))
        self.assertEqual(first["address"], "58 Elm St., Apt. 108, Lowell, MA 01852")
        self.assertEqual(first["url"], "https://x.appfolio.com/listings/detail/aaaa1111")
        self.assertEqual(first["available"], "12/1/26")
        self.assertEqual((first["lat"], first["lng"]), (42.6401, -71.3101))
        self.assertEqual(raws[2]["beds"], 0.0)  # studio

    def test_json_ld_needs_a_rent(self):
        raws, method = structured.extract((FIXTURES / "jsonld_page.html").read_text(), PAGE)
        self.assertEqual(method, "structured data")
        self.assertEqual(len(raws), 1)  # the entry without a rent is left out
        self.assertEqual((raws[0]["price"], raws[0]["beds"], raws[0]["sqft"]), (1850, 2.0, 900))
        self.assertEqual(raws[0]["url"], "https://pm.example.test/units/12-pine-3")
        self.assertEqual(raws[0]["town"], "Lowell")

    def test_rentpress_page_gives_one_listing_per_community_with_something_open(self):
        html = (FIXTURES / "rentpress_search.html").read_text()
        raws, method = structured.extract(html, "https://apts.example.test/search/")
        self.assertEqual(method, "RentPress community data")
        self.assertEqual([r["title"] for r in raws], ["River Towers", "Harbor Flats"])  # nothing open at the Mill
        river = raws[0]
        self.assertEqual([(u["beds"], u["price_low"]) for u in river["units"]], [(1.0, 1385), (2.0, 1631)])
        self.assertEqual(river["address"], "1 River Place, Lowell, MA 01852")
        self.assertEqual(river["available"], "36 units available")
        self.assertIn("Cat Friendly", river["description"])
        self.assertEqual(river["amenities"], ["Heat Included", "Onsite Parking"])
        self.assertEqual(raws[1]["units"][0]["beds"], 0.0)  # studio

    def test_rentpress_rent_change_changes_the_page_fingerprint(self):
        html = (FIXTURES / "rentpress_search.html").read_text()
        text = ai_sources.page_to_text(html, PAGE)
        self.assertNotEqual(ai_sources._page_hash(html, text),
                            ai_sources._page_hash(html.replace("1631", "1699"), text))

    def test_page_without_structured_listings(self):
        self.assertIsNone(structured.extract(PROSE.format(body=""), PAGE))


class ReadPageTest(DbTest):
    watch = {"id": 7, "url": PAGE, "label": "PM", "render": 0, "last_hash": None, "state": None}

    def read(self, fetcher, **changes):
        return ai_sources.read_page({**self.watch, **changes}, self.settings, fetcher)

    def test_challenge_page_is_reported_as_blocked_not_as_needing_a_browser(self):
        page = self.read(serve("<html><head><title>Client Challenge</title></head><body>"
                               "<p>A required part of this site couldn't load.</p></body></html>"))
        self.assertEqual(page.state, "blocked")

    def test_rentpress_community_keeps_every_floor_plan_and_your_towns_only(self):
        page = self.read(serve((FIXTURES / "rentpress_search.html").read_text()))
        self.assertEqual(page.state, "ok")
        self.assertEqual([i["title"] for i in page.items], ["River Towers"])  # Portland is not on the list
        self.assertEqual(len(page.items[0]["units"]), 2)
        self.assertEqual(page.items[0]["amenities"], ["Heat Included", "Onsite Parking"])

    def test_appfolio_keeps_only_listings_in_your_towns(self):
        page = self.read(serve((FIXTURES / "appfolio_listings.html").read_text()))
        self.assertEqual(page.state, "ok")
        self.assertEqual(sorted(i["title"] for i in page.items), ["58 Elm St., Apt. 108", "58 Elm St., Apt. 210"])
        self.assertIn("1 more outside your towns", page.status)
        self.assertTrue(all(i["scope"] == "w7" and i["source_id"].startswith("w7-") for i in page.items))

    def test_successful_read_with_no_vacancies(self):
        page = self.read(serve((FIXTURES / "appfolio_empty.html").read_text()))
        self.assertEqual((page.state, page.items), ("empty", []))
        self.assertIsNotNone(page.page_hash)

    def test_blocked_page(self):
        for fetcher in (serve("Forbidden", 403), serve("<html><title>Just a moment...</title><body>Verify you are human</body></html>")):
            page = self.read(fetcher)
            self.assertEqual(page.state, "blocked")
            self.assertIsNone(page.page_hash)

    def test_page_that_needs_javascript_is_incomplete(self):
        page = self.read(serve('<html><body><div id="root"></div><noscript>You need to enable JavaScript.</noscript></body></html>'))
        self.assertEqual(page.state, "incomplete")
        self.assertIn("browser rendering", page.status)

    def test_browser_requested_but_not_installed(self):
        with mock.patch.object(render, "available", lambda: False):
            page = ai_sources.read_page({**self.watch, "render": 1}, self.settings, serve("x"), render.Browser())
        self.assertEqual(page.state, "failed")
        self.assertIn("not installed", page.status)

    def test_model_failure_is_not_an_empty_page(self):
        def boom(*a, **k):
            raise RuntimeError("the model returned listings in an unreadable form (ValueError)")
        with mock.patch.multiple(ai, status=lambda *a, **k: {"ok": True}, extract_listings=boom):
            page = self.read(serve(PROSE.format(body="<p>2 bed, $1,800/mo</p>")))
        self.assertEqual(page.state, "failed")
        self.assertIsNone(page.page_hash)

    def test_model_unreachable(self):
        with mock.patch.object(ai, "status", lambda *a, **k: {"ok": False}):
            page = self.read(serve(PROSE.format(body="<p>2 bed, $1,800/mo</p>")))
        self.assertEqual(page.state, "failed")
        self.assertIn("local model is not reachable", page.status)

    def test_rents_on_the_page_but_nothing_extracted_is_a_failure(self):
        with model([]):
            page = self.read(serve(PROSE.format(body="<p>Sunny two bedroom, $1,800 a month, call today</p>")))
        self.assertEqual(page.state, "failed")

    def test_model_finds_nothing_on_a_page_that_says_so(self):
        with model([]):
            page = self.read(serve(PROSE.format(body="<p>Sorry, no vacancies. Rents start at $1,800.</p>")))
        self.assertEqual(page.state, "empty")

    def test_model_listings(self):
        with model([{"title": "5 Pine St #3", "town": "Methuen", "address": "5 Pine St #3, Methuen", "price": 1600, "beds": 1}]):
            page = self.read(serve(PROSE.format(body="<p>5 Pine St #3, Methuen, 1 bed, $1,600/mo</p>")))
        self.assertEqual((page.state, len(page.items)), ("ok", 1))
        self.assertEqual(page.items[0]["town"], "Methuen")


class WatchHuntTest(DbTest):
    """The whole path: page status rows, caching, and what happens to stored listings."""

    def setUp(self):
        super().setUp()
        with db.connect() as conn:
            self.watch_id = conn.execute("INSERT INTO watch_urls (url, label) VALUES (?, 'PM')", (PAGE,)).lastrowid
        self.settings["sources"]["craigslist"]["enabled"] = False
        self.settings["sources"]["rentcom"]["enabled"] = False
        db.save_settings(self.settings)

    def hunt(self, body: str, status: int = 200) -> dict:
        with db.connect() as conn:  # tests run hunts back to back; real ones are hours apart
            conn.execute("UPDATE listings SET last_seen='2026-01-01T00:00:00+00:00'")
        with mock.patch.object(hunt, "Fetcher", lambda *a, **k: serve(body, status)):
            return hunt.run("test")

    def watch_row(self) -> dict:
        with db.connect() as conn:
            return dict(conn.execute("SELECT * FROM watch_urls WHERE id=?", (self.watch_id,)).fetchone())

    def listings(self) -> list[dict]:
        with db.connect() as conn:
            return [db.row_to_listing(r) for r in conn.execute("SELECT * FROM listings ORDER BY id")]

    def test_blocked_page_keeps_listings_and_is_not_cached(self):
        good = (FIXTURES / "appfolio_listings.html").read_text()
        first = self.hunt(good)
        self.assertEqual(first["status"], "ok")
        row = self.watch_row()
        self.assertEqual((row["state"], row["last_count"]), ("ok", 2))
        good_hash, good_time = row["last_hash"], row["last_success"]
        self.assertTrue(good_hash and good_time)

        for _ in range(4):  # more failed hunts than the expiry limit
            run = self.hunt("Forbidden", 403)
        self.assertEqual(run["status"], "failed")  # the only source could not be checked
        self.assertIn("refused", run["sources"]["watch"]["problems"][0])
        row = self.watch_row()
        self.assertEqual(row["state"], "blocked")
        self.assertEqual((row["last_hash"], row["last_success"]), (good_hash, good_time))
        for listing in self.listings():
            self.assertTrue(listing["active"])
            self.assertTrue(listing["unverified"])
            self.assertEqual(listing["missed_runs"], 0)

        self.hunt(good)  # the page is back, identical to the last good read
        self.assertEqual(self.watch_row()["state"], "ok")
        self.assertTrue(all(not l["unverified"] and l["active"] for l in self.listings()))

    def test_unchanged_page_keeps_listings_alive(self):
        good = (FIXTURES / "appfolio_listings.html").read_text()
        for _ in range(5):
            run = self.hunt(good)
        self.assertEqual(run["status"], "ok")
        self.assertIn("unchanged", self.watch_row()["last_status"])
        self.assertTrue(all(l["active"] and l["missed_runs"] == 0 for l in self.listings()))

    def test_confirmed_empty_page_expires_listings_by_the_usual_policy(self):
        self.hunt((FIXTURES / "appfolio_listings.html").read_text())
        empty = (FIXTURES / "appfolio_empty.html").read_text()
        self.hunt(empty)
        self.assertEqual(self.watch_row()["state"], "empty")
        self.assertEqual({l["missed_runs"] for l in self.listings()}, {1})
        self.assertTrue(all(l["active"] and not l["unverified"] for l in self.listings()))
        # An unchanged empty page is still a successful check, so the count keeps rising.
        self.hunt(empty)
        self.hunt(empty)
        self.assertTrue(all(not l["active"] for l in self.listings()))

    def test_failed_extraction_is_never_remembered_as_unchanged(self):
        body = PROSE.format(body="<p>5 Pine St #3, Methuen, 1 bed, $1,600/mo</p>")

        def boom(*a, **k):
            raise RuntimeError("unreadable")
        with mock.patch.multiple(ai, status=lambda *a, **k: {"ok": True}, extract_listings=boom):
            self.hunt(body)
            self.hunt(body)
        row = self.watch_row()
        self.assertEqual((row["state"], row["last_hash"], row["last_success"]), ("failed", None, None))
        with model([{"title": "5 Pine St #3", "town": "Methuen", "address": "5 Pine St #3, Methuen", "price": 1600, "beds": 1}]):
            run = self.hunt(body)  # same page, working model: it must be read, not skipped
        self.assertEqual(run["sources"]["watch"]["found"], 1)
        self.assertEqual(self.watch_row()["state"], "ok")

    def test_page_cached_before_the_update_is_read_again(self):
        """A hash stored by the old code (no state) says nothing about whether that read was good."""
        with db.connect() as conn:
            conn.execute("UPDATE watch_urls SET last_hash='stale' WHERE id=?", (self.watch_id,))
        run = self.hunt((FIXTURES / "jsonld_page.html").read_text())
        self.assertEqual(run["sources"]["watch"]["found"], 1)
