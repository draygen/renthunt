"""Moving fast: phone alerts, quick checks between full hunts, and unknown facts in the score."""
from datetime import datetime, timedelta
from unittest import mock

import httpx

from renthunt import db, hunt, scoring
from renthunt.sources import craigslist, rentcom
from renthunt.sources.base import SourceResult

from . import DbTest


def unit_listing(**fields) -> dict:
    return {"title": "23 Pratt Ave Apt 6", "town": "Lowell", "description": "", "amenities": [], "images": [],
            "units": [{"beds": 2.0, "price_low": 1850}], "first_seen": datetime.now().astimezone().isoformat(),
            **fields}


class UnknownIsNotNoTest(DbTest):
    def setUp(self):
        super().setUp()
        self.settings["criteria"].update(cats=True, cat_count=2, move_in="asap", max_price=2000)

    def score(self, listing, building=""):
        return scoring.evaluate(listing, self.settings, None, building)

    def test_bare_listing_is_tagged_to_ask_rather_than_pushed_down(self):
        bare = self.score(unit_listing())
        self.assertTrue(bare["is_match"])
        for flag in ("pet policy unknown", "move-in date not stated", "features not listed"):
            self.assertIn(flag, bare["flags"])
        told = self.score(unit_listing(description="Third floor walk-up with hardwood floors, a new kitchen and "
                                                   "a sunny living room close to downtown."))
        self.assertGreater(bare["score"], told["score"])   # saying nothing beats confirming it has none
        confirmed = self.score(unit_listing(description="Available now. Cats OK. Heat included, parking, laundry."))
        self.assertGreater(confirmed["score"], bare["score"])  # but a confirmed place still comes first

    def test_building_listing_fills_in_pets_and_features(self):
        building = "Hadley Park Apartments. Pets: Cat Friendly. Heat Included, Onsite Parking"
        result = self.score(unit_listing(), building)
        self.assertEqual(result["cats_ok"], "yes")
        self.assertIn("cats OK at this building", result["reasons"])
        self.assertNotIn("pet policy unknown", result["flags"])
        self.assertGreater(result["score"], self.score(unit_listing())["score"])

    def test_building_saying_no_cats_is_a_warning_not_a_verdict(self):
        result = self.score(unit_listing(), "Lovely building. No pets.")
        self.assertTrue(result["is_match"])
        self.assertIn("another listing at this address says no cats", result["flags"])

    def test_what_the_listing_says_itself_wins(self):
        result = self.score(unit_listing(description="Sorry, no pets."), "Pets: Cat Friendly")
        self.assertFalse(result["is_match"])

    def test_neighbours_are_found_by_address_including_number_ranges(self):
        community = self.add_listing(source="watch", title="Westford Park", address="1001-1009 Westford Street, Lowell, MA",
                                     description="Pets: Cat Friendly")
        unit = self.add_listing(source="rentcast", title="1005 Westford St Apt 19", address="1005 Westford St, Apt 19, Lowell, MA")
        other = self.add_listing(source="rentcast", title="88 Westford St", address="88 Westford St, Lowell, MA")
        self.settings["criteria"]["cats"] = True
        db.save_settings(self.settings)
        hunt.rescore_all()
        self.assertEqual(self.row(unit)["cats_ok"], "yes")
        self.assertEqual(self.row(other)["cats_ok"], "unknown")
        self.assertEqual(self.row(community)["cats_ok"], "yes")


class QuickCheckTest(DbTest):
    def setUp(self):
        super().setUp()
        self.settings["sources"]["craigslist"]["detail_fetch_limit"] = 0
        db.save_settings(self.settings)

    def test_quick_check_reads_only_its_sources_and_leaves_the_rest_alone(self):
        kept = self.add_listing(source="rentcom", last_seen="2026-01-01T00:00:00+00:00")
        asked = []
        with mock.patch.object(craigslist, "fetch", lambda *a: asked.append("craigslist") or SourceResult()), \
             mock.patch.object(rentcom, "fetch", lambda *a: asked.append("rentcom") or SourceResult(checked={"*"})):
            run = hunt.run("quick", ["craigslist", "watch", "mail"])
        self.assertEqual(asked, ["craigslist"])
        self.assertEqual(list(run["sources"]), ["craigslist"])
        self.assertEqual((self.row(kept)["active"], self.row(kept)["missed_runs"]), (True, 0))

    def finish_run(self, trigger: str, minutes_ago: float) -> None:
        at = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat()
        with db.connect() as conn:
            conn.execute("INSERT INTO runs (started_at, finished_at, trigger, status) VALUES (?, ?, ?, 'ok')",
                         (at, at, trigger))

    def test_quick_checks_do_not_postpone_the_full_hunt(self):
        self.settings["schedule"].update(interval_hours=4, quick_minutes=30, quiet_start=0, quiet_end=0)
        self.finish_run("schedule", 200)
        self.finish_run("quick", 5)
        now = datetime.now().astimezone()
        full, quick = hunt.next_run_time(self.settings), hunt.next_quick_time(self.settings)
        self.assertAlmostEqual((full - now).total_seconds() / 60, 40, delta=1)   # 4 h after the full hunt
        self.assertAlmostEqual((quick - now).total_seconds() / 60, 25, delta=1)  # 30 min after the latest run
        self.settings["schedule"]["quick_minutes"] = 0
        self.assertIsNone(hunt.next_quick_time(self.settings))


class PushTest(DbTest):
    def listing(self, n: int, score: int) -> dict:
        return {"title": f"{n} Pine St", "url": f"https://example.test/{n}", "town": "Lowell", "price": 1800,
                "match_price": 1800, "beds_min": 2.0, "score": score, "reasons": ["$1,800 for 2 bed", "cats OK"],
                "flags": ["confirm 2 cats are OK"]}

    def test_ntfy_gets_one_tappable_notification_per_listing_best_last(self):
        sent = []
        post = lambda url, **kw: sent.append((url, kw["json"])) or httpx.Response(200, request=httpx.Request("POST", url))  # noqa: E731
        with mock.patch.object(httpx, "post", post):
            hunt.push("https://ntfy.sh/my-topic", [self.listing(n, 90 - 10 * n) for n in range(7)])
        self.assertEqual({url for url, _ in sent}, {"https://ntfy.sh/"})
        self.assertEqual({m["topic"] for _, m in sent}, {"my-topic"})
        self.assertEqual(len(sent), hunt.PUSH_EACH + 1)          # five of their own, two in a summary
        best = sent[-1][1]
        self.assertEqual((best["click"], best["priority"]), ("https://example.test/0", 4))
        self.assertIn("$1,800 · 2 bd · Lowell", best["title"])
        self.assertIn("cats OK", best["message"])
        self.assertIn("2 more", sent[0][1]["title"])

    def test_address_without_a_topic_is_refused(self):
        with self.assertRaises(ValueError):
            hunt.push("ntfy.sh", [self.listing(1, 80)])
