"""Tests run against a throwaway data folder and never touch the network.

Run them with:  .venv/bin/python -m unittest discover -s tests -t . -v
"""
import os
import tempfile
import unittest
import uuid
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="renthunt-test-")
os.environ["RENTHUNT_DATA"] = _TMP

from renthunt import config, db  # noqa: E402

for _secret in ("RENTCAST_API_KEY", "RENTHUNT_MAIL_USER", "RENTHUNT_MAIL_PASSWORD"):
    os.environ.pop(_secret, None)  # real secrets in .env must never reach a test
FIXTURES = Path(__file__).parent / "fixtures"


class DbTest(unittest.TestCase):
    """Each test gets its own empty database."""

    def setUp(self):
        config.DATA_DIR = Path(_TMP)
        config.DB_PATH = Path(_TMP) / f"{uuid.uuid4().hex}.db"
        db.init()
        self.settings = db.get_settings()

    def add_listing(self, **fields) -> int:
        """Store a listing directly; returns its id."""
        from renthunt import hunt
        item = {"source": "craigslist", "source_id": uuid.uuid4().hex[:10], "url": "https://example.test/x",
                "title": "A place", "town": "Lowell", "state": "MA",
                "units": [{"beds": fields.pop("beds", 2.0), "price_low": fields.pop("price", 1800),
                           "price_high": None, "sqft": None}]}
        marks = {k: fields.pop(k) for k in ("favorite", "notes", "status", "hidden", "active", "last_seen",
                                            "missed_runs", "alerted") if k in fields}
        item.update(fields)
        with db.connect() as conn:
            listing_id, _ = hunt.upsert(conn, item, marks.pop("last_seen", "2026-11-01T00:00:00+00:00"))
            if marks:
                sets = ", ".join(f"{k}=?" for k in marks)
                conn.execute(f"UPDATE listings SET {sets} WHERE id=?", [*marks.values(), listing_id])
        return listing_id

    def row(self, listing_id: int) -> dict:
        with db.connect() as conn:
            return db.row_to_listing(conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone())
