"""RentCast against recorded-shape fixtures. Nothing here talks to the real API."""
import json
import os
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import httpx

from renthunt import db, hunt
from renthunt.sources import rentcast

from . import FIXTURES, DbTest

KEY = "test-key-do-not-leak-0123456789"
RECORDS = json.loads((FIXTURES / "rentcast_listings.json").read_text())


def record(n: int, city: str = "Lowell") -> dict:
    return {"id": f"{n}-Main-St,-{city},-MA", "formattedAddress": f"{n} Main St, {city}, MA 01852",
            "addressLine1": f"{n} Main St", "city": city, "state": "MA", "bedrooms": 2, "bathrooms": 1,
            "price": 1500 + n, "status": "Active"}


class RentcastTest(DbTest):
    def setUp(self):
        super().setUp()
        os.environ[rentcast.KEY_ENV] = KEY
        self.addCleanup(os.environ.pop, rentcast.KEY_ENV, None)
        for town in self.settings["towns"]:
            town["enabled"] = town["name"] in ("Lowell", "Dracut", "Chelmsford")
        self.cfg = self.settings["sources"]["rentcast"]
        self.cfg["enabled"] = True
        self.requests: list[httpx.Request] = []
        self.logs: list[str] = []

    def fetch(self, handler, **kw):
        def wrapped(request):
            self.requests.append(request)
            return handler(request)
        return rentcast.fetch(self.settings, None, self.logs.append, transport=httpx.MockTransport(wrapped), **kw)

    def by_city(self, pages: dict):
        """Handler serving {city: [records]} with real limit/offset behaviour."""
        def handler(request):
            q = request.url.params
            found = pages.get(q["city"], [])
            return httpx.Response(200, json=found[int(q["offset"]):int(q["offset"]) + int(q["limit"])])
        return handler

    def test_request_and_mapping(self):
        result = self.fetch(self.by_city({"Lowell": RECORDS}))
        first = self.requests[0]
        self.assertEqual(str(first.url).split("?")[0], "https://api.rentcast.io/v1/listings/rental/long-term")
        self.assertEqual(first.headers["x-api-key"], KEY)
        q = first.url.params
        self.assertEqual((q["city"], q["state"], q["status"], q["limit"], q["offset"]), ("Lowell", "MA", "Active", "500", "0"))
        self.assertEqual((q["price"], q["bedrooms"]), ("500:2500", "1:3"))  # from the default criteria
        self.assertNotIn(KEY, str(first.url))

        self.assertEqual(len(result.items), 2)  # the Springfield record is not in a configured town
        item = result.items[0]
        self.assertEqual(item["source"], "rentcast")
        self.assertEqual(item["source_id"], "12-Pine-St,-Unit-3,-Lowell,-MA-01851")
        self.assertEqual(item["title"], "12 Pine St Unit 3")
        self.assertEqual((item["town"], item["state"], item["scope"]), ("Lowell", "MA", "town:Lowell"))
        self.assertEqual(item["units"], [{"beds": 2.0, "price_low": 1850, "price_high": None, "sqft": 900}])
        self.assertEqual((item["baths"], item["lat"], item["posted_at"]), (1, 42.63, "2026-11-02T00:00:00.000Z"))
        self.assertIn("Pat Example", item["contact"])
        self.assertTrue(item["url"].startswith("https://www.google.com/maps/search/"))
        self.assertEqual(result.checked, {"town:Lowell", "town:Dracut", "town:Chelmsford"})

    def test_pagination(self):
        with mock.patch.object(rentcast, "PAGE_SIZE", 2):
            result = self.fetch(self.by_city({"Lowell": [record(n) for n in range(1, 4)]}))
        lowell = [(r.url.params["offset"], r.url.params["limit"]) for r in self.requests if r.url.params["city"] == "Lowell"]
        self.assertEqual(lowell, [("0", "2"), ("2", "2")])
        self.assertEqual(len(result.items), 3)
        self.assertIn("town:Lowell", result.checked)

    def test_more_pages_than_allowed_is_a_partial_town(self):
        self.cfg["max_pages_per_town"] = 2
        with mock.patch.object(rentcast, "PAGE_SIZE", 2):
            result = self.fetch(self.by_city({"Lowell": [record(n) for n in range(1, 8)]}))
        self.assertEqual(len(result.items), 4)
        self.assertNotIn("town:Lowell", result.checked)
        self.assertIn("only the first 2 page(s)", result.failed["town:Lowell"])

    def test_404_means_no_results_not_a_failure(self):
        result = self.fetch(lambda request: httpx.Response(404, json={"status": 404, "error": "resource/not-found"}))
        self.assertEqual((result.items, result.failed), ([], {}))
        self.assertEqual(len(result.checked), 3)

    def test_bad_key_stops_after_one_request(self):
        result = self.fetch(lambda request: httpx.Response(401, json={"error": "auth/api-key-invalid"}))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(result.failed), 3)
        self.assertIn("rejected the API key", result.failed["town:Lowell"])

    def test_quota_or_billing_refusal_stops_the_source(self):
        result = self.fetch(lambda request: httpx.Response(403, json={"error": "billing/subscription-inactive"}))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("quota", result.failed["town:Dracut"])

    def test_rate_limit_stops_the_source(self):
        result = self.fetch(lambda request: httpx.Response(429, json={"error": "auth/rate-limit-exceeded"}))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("429", result.failed["town:Lowell"])

    def test_timeouts_fail_towns_and_then_give_up(self):
        def slow(request):
            raise httpx.ReadTimeout("timed out", request=request)
        result = self.fetch(slow)
        self.assertEqual(len(self.requests), 2)  # two in a row, then it stops spending
        self.assertIn("too long", result.failed["town:Lowell"])
        self.assertEqual(len(result.failed), 3)

    def test_one_town_failing_does_not_fail_the_others(self):
        def handler(request):
            if request.url.params["city"] == "Dracut":
                return httpx.Response(500, text="oops")
            return httpx.Response(200, json=[record(1, request.url.params["city"])])
        result = self.fetch(handler)
        self.assertEqual(result.checked, {"town:Lowell", "town:Chelmsford"})
        self.assertEqual(list(result.failed), ["town:Dracut"])

    def test_per_hunt_budget_and_rotation(self):
        self.cfg["max_requests_per_hunt"] = 2
        handler = self.by_city({})
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        first = self.fetch(handler, now=now)
        self.assertEqual([r.url.params["city"] for r in self.requests], ["Lowell", "Dracut"])
        self.assertEqual(first.skipped, {"town:Chelmsford": "request budget for this hunt used up"})
        self.requests.clear()
        second = self.fetch(handler, now=now + timedelta(hours=1))
        # Only the town that has not been checked is asked about; the others are not due.
        self.assertEqual([r.url.params["city"] for r in self.requests], ["Chelmsford"])
        self.assertEqual(second.checked, {"town:Chelmsford"})
        self.requests.clear()
        self.fetch(handler, now=now + timedelta(hours=2))
        self.assertEqual(self.requests, [])
        self.fetch(handler, now=now + timedelta(hours=169))
        self.assertEqual([r.url.params["city"] for r in self.requests], ["Lowell", "Dracut"])

    def test_monthly_budget_is_a_hard_stop(self):
        self.cfg["max_requests_per_month"] = 4
        db.set_state(rentcast.USAGE_STATE, {"period": rentcast.period_start(1), "count": 3})
        result = self.fetch(self.by_city({}))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(rentcast.usage(self.cfg)["count"], 4)
        self.assertEqual(len(result.skipped), 2)
        self.requests.clear()
        again = self.fetch(self.by_city({}), now=datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(self.requests, [])
        self.assertIn("monthly request budget used up", again.note)
        self.assertEqual(again.failed, {})

    def test_failed_requests_count_against_the_budget(self):
        self.fetch(lambda request: httpx.Response(500))
        self.assertEqual(rentcast.usage(self.cfg)["count"], 2)

    def test_billing_period(self):
        self.assertEqual(rentcast.period_start(1, date(2026, 11, 13)), "2026-11-01")
        self.assertEqual(rentcast.period_start(20, date(2026, 11, 13)), "2026-10-20")
        self.assertEqual(rentcast.period_start(20, date(2026, 1, 5)), "2025-12-20")
        db.set_state(rentcast.USAGE_STATE, {"period": "2020-01-01", "count": 999})
        self.assertEqual(rentcast.usage(self.cfg)["count"], 0)  # an old month does not carry over

    def test_key_never_appears_in_logs_results_or_status(self):
        def echo(request):  # a server that echoes the key back in its error
            return httpx.Response(500, text=f"bad key {KEY}")
        result = self.fetch(echo)
        run_text = json.dumps([self.logs, result.failed, result.note, rentcast.status(self.settings)])
        self.assertNotIn(KEY, run_text)
        self.assertTrue(rentcast.status(self.settings)["key_present"])

    def test_enabled_without_a_key_is_reported_not_silently_skipped(self):
        os.environ.pop(rentcast.KEY_ENV)
        self.settings["sources"]["craigslist"]["enabled"] = False
        self.settings["sources"]["rentcom"]["enabled"] = False
        db.save_settings(self.settings)
        kept = self.add_listing(source="rentcast", scope="town:Lowell")
        run = hunt.run("test")
        self.assertEqual(run["status"], "failed")
        self.assertIn("RENTCAST_API_KEY", run["sources"]["rentcast"]["error"])
        self.assertTrue(self.row(kept)["active"] and self.row(kept)["unverified"])

    def test_off_by_default(self):
        from renthunt import config
        self.assertFalse(config.DEFAULT_SETTINGS["sources"]["rentcast"]["enabled"])
