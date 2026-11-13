"""Duplicates across sources: what merges, what must not, and what survives a merge."""
from renthunt import db, dedupe, hunt

from . import DbTest


def listing(i, **f):
    base = {"id": i, "source": "craigslist", "title": "Nice place", "town": "Lowell", "state": "MA",
            "address": None, "price": 1800, "beds_min": 2.0, "baths": None, "sqft": None,
            "units": [{"beds": 2.0, "price_low": 1800}], "active": True, "favorite": False,
            "hidden": False, "notes": "", "status": "new"}
    return {**base, **f}


def dups(*listings):
    return {i: info["dup_of"] for i, info in dedupe.group(list(listings)).items()}


class AddressTest(DbTest):
    def test_normalizing(self):
        home = dedupe.home_of(listing(1, address="58 Elm Street, Apt. 108, Lowell, MA 01852"))
        self.assertEqual((home.number, home.street, home.town, home.unit), ("58", "elm st", "lowell", "108"))
        same = dedupe.home_of(listing(2, address="58 ELM ST #108, Lowell, MA"))
        self.assertEqual((home.building, home.unit), (same.building, same.unit))
        self.assertEqual(dedupe.home_of(listing(3, address="420 Boston Rd unit R, Lowell, MA 01821")).unit, "r")
        self.assertEqual(dedupe.home_of(listing(4, address="12 North Main Avenue 2nd Floor, Lowell")).unit, "fl2")
        self.assertEqual(dedupe.home_of(listing(4, address="12 N Main Ave, Lowell")).street, "n main ave")

    def test_no_house_number_means_no_address(self):
        self.assertIsNone(dedupe.home_of(listing(1, address="Washington St, Lowell, MA 01852")))
        self.assertIsNone(dedupe.home_of(listing(1, address="Lowell, MA 01852", title="Sunny 2 bed near train")))

    def test_address_in_the_title(self):
        home = dedupe.home_of(listing(1, address="Lowell, MA 01852", title="140 Bailey St"))
        self.assertEqual((home.number, home.street), ("140", "bailey st"))
        self.assertIsNone(dedupe.home_of(listing(2, title="2 bedroom apartment with parking and laundry")))


class GroupingTest(DbTest):
    def test_same_unit_on_two_sources_is_one_home(self):
        out = dups(listing(1, address="12 Pine St Unit 3, Lowell, MA"),
                   listing(2, source="rentcast", address="12 Pine Street, Unit 3, Lowell, MA 01851", price=1850))
        self.assertEqual(out, {1: None, 2: 1})

    def test_different_units_in_one_building_stay_separate(self):
        out = dups(listing(1, address="12 Pine St Unit 3, Lowell, MA"),
                   listing(2, source="rentcast", address="12 Pine St Unit 4, Lowell, MA"),
                   listing(3, source="watch", address="12 Pine St #5, Lowell, MA"))
        self.assertEqual(out, {1: None, 2: None, 3: None})

    def test_same_title_and_rent_on_two_sources_is_not_enough(self):
        out = dups(listing(1, title="Sunny 2 bed", address=None),
                   listing(2, source="rentcast", title="Sunny 2 bed", address=None),
                   listing(3, source="watch", title="Sunny 2 bed", address="Elm St, Lowell"))
        self.assertEqual(out, {1: None, 2: None, 3: None})

    def test_craigslist_reposts_without_an_address_still_fold(self):
        self.assertEqual(dups(listing(1, title="Sunny 2 bed!"), listing(2, title="SUNNY 2 BED")), {1: None, 2: 1})

    def test_no_unit_needs_matching_details(self):
        a = listing(1, address="9 Oak Rd, Lowell, MA", baths=1.0)
        self.assertEqual(dups(a, listing(2, source="rentcast", address="9 Oak Road, Lowell", price=1825, baths=1.0)), {1: None, 2: 1})
        self.assertEqual(dups(a, listing(2, source="rentcast", address="9 Oak Road, Lowell", beds_min=3.0))[2], None)
        self.assertEqual(dups(a, listing(2, source="rentcast", address="9 Oak Road, Lowell", price=2300))[2], None)
        self.assertEqual(dups(a, listing(2, source="rentcast", address="9 Oak Road, Lowell", baths=2.0))[2], None)
        self.assertEqual(dups(a, listing(2, source="rentcast", address="9 Oak Road, Lowell", beds_min=None))[2], None)

    def test_same_street_in_another_town_is_another_home(self):
        out = dups(listing(1, address="9 Oak Rd Unit 2, Lowell, MA"),
                   listing(2, source="rentcast", town="Dracut", address="9 Oak Rd Unit 2, Dracut, MA"))
        self.assertEqual(out, {1: None, 2: None})

    def test_listing_without_a_unit_is_not_guessed_into_one_of_two_units(self):
        vague = listing(1, address="12 Pine St, Lowell, MA")
        unit3 = listing(2, source="rentcast", address="12 Pine St Unit 3, Lowell, MA")
        unit4 = listing(3, source="rentcast", address="12 Pine St Unit 4, Lowell, MA")
        self.assertEqual(dups(vague, unit3, unit4), {1: None, 2: None, 3: None})
        self.assertEqual(dups(vague, unit3), {1: None, 2: 1})  # with one candidate it is a match

    def test_community_is_not_merged_with_a_unit_inside_it(self):
        community = listing(1, source="rentcom", address="100 Mill St, Lowell, MA", beds_min=1.0, price=1700,
                            units=[{"beds": 1.0, "price_low": 1700}, {"beds": 2.0, "price_low": 2100}])
        unit = listing(2, source="rentcast", address="100 Mill St Apt 305, Lowell, MA", beds_min=1.0, price=1700)
        bare = listing(3, source="craigslist", address="100 Mill St, Lowell, MA", beds_min=1.0, price=1700)
        self.assertEqual(dups(community, unit, bare)[1], None)
        self.assertEqual(dups(community, unit)[2], None)
        self.assertEqual(dups(community, bare)[3], None)

    def test_the_copy_you_worked_with_is_the_one_kept(self):
        out = dups(listing(1, address="12 Pine St Unit 3, Lowell"),
                   listing(2, source="rentcast", address="12 Pine St Unit 3, Lowell", favorite=True, notes="called Pat"))
        self.assertEqual(out, {1: 2, 2: None})

    def test_inactive_listings_are_not_grouped(self):
        out = dups(listing(1, address="12 Pine St Unit 3, Lowell", active=False),
                   listing(2, source="rentcast", address="12 Pine St Unit 3, Lowell"))
        self.assertEqual(out, {1: None, 2: None})


class StoredTest(DbTest):
    """Through the database: nothing is lost and nobody is alerted twice."""

    def test_copies_keep_their_rows_links_and_history(self):
        first = self.add_listing(address="12 Pine St Unit 3, Lowell, MA", url="https://cl.test/1", price=1800,
                                 favorite=1, notes="tour Friday", status="touring", alerted=1)
        with db.connect() as conn:  # a later rent change on the first copy
            item = {"source": "craigslist", "source_id": self.row(first)["source_id"], "url": "https://cl.test/1",
                    "title": "A place", "units": [{"beds": 2.0, "price_low": 1750, "price_high": None, "sqft": None}]}
            hunt.upsert(conn, item, "2026-11-05T00:00:00+00:00")
        second = self.add_listing(source="rentcast", address="12 Pine St, Unit 3, Lowell, MA 01851",
                                  url="https://maps.test/2", price=1750)
        hunt.rescore_all()

        kept, copy = db.get_listing(first), db.get_listing(second)
        self.assertEqual(copy["dup_of"], first)
        self.assertIsNone(kept["dup_of"])
        self.assertEqual((kept["favorite"], kept["notes"], kept["status"]), (True, "tour Friday", "touring"))
        self.assertEqual([p["price"] for p in kept["price_history"]], [1800, 1750])
        self.assertEqual([p["price"] for p in copy["price_history"]], [1750])
        self.assertEqual([(c["source"], c["url"]) for c in kept["also_listed"]], [("rentcast", "https://maps.test/2")])
        self.assertEqual([(c["source"], c["url"]) for c in copy["also_listed"]], [("craigslist", "https://cl.test/1")])
        shown, _ = db.query_listings(view="all")
        self.assertEqual([l["id"] for l in shown], [first])

    def test_a_matched_copy_does_not_alert_again(self):
        self.settings["notify"]["min_score"] = 0
        self.add_listing(address="12 Pine St Unit 3, Lowell, MA", price=1800)
        hunt.rescore_all()
        self.assertEqual(len(hunt._send_alerts(self.settings, print)), 1)
        self.add_listing(source="rentcast", address="12 Pine St, Unit 3, Lowell, MA 01851", price=1800)
        self.add_listing(source="rentcast", address="12 Pine St, Unit 4, Lowell, MA 01851", price=1800)
        hunt.rescore_all()
        alerts = hunt._send_alerts(self.settings, print)
        self.assertEqual([a["address"] for a in alerts], ["12 Pine St, Unit 4, Lowell, MA 01851"])  # the other unit is news

    def test_marks_move_to_the_surviving_copy(self):
        gone = self.add_listing(address="12 Pine St Unit 3, Lowell, MA", favorite=1, notes="love it",
                                status="contacted", alerted=1, active=0, last_seen=db.now())
        fresh = self.add_listing(source="rentcast", address="12 Pine St, Unit 3, Lowell, MA 01851")
        other = self.add_listing(source="rentcast", address="12 Pine St, Unit 4, Lowell, MA 01851")
        hunt.rescore_all()
        row = self.row(fresh)
        self.assertEqual((row["favorite"], row["notes"], row["status"], row["alerted"]), (True, "love it", "contacted", True))
        self.assertEqual((self.row(other)["favorite"], self.row(other)["status"]), (False, "new"))
        self.assertEqual(self.row(gone)["notes"], "love it")  # the original is untouched
