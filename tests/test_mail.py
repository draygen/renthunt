"""Email alerts: which messages are read, what is kept, and that the mailbox is left alone."""
import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest import mock

from renthunt import ai, db
from renthunt.sources import mail

from . import DbTest

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
TRACKER = "https://click.mail.zillow.example/f/a/" + "x" * 300

ALERT = f"""<html><head><title>t</title><style>p{{}}</style></head><body>
<p>2 new rentals match your saved search for Lowell and the surrounding area this morning.</p>
<a href="{TRACKER}1">12 Pine St APT 3, Lowell, MA 01852</a> <p>$1,850/mo 2 bd 1 ba</p>
<a href="{TRACKER}2">9 Wharf St, Portland, ME 04101</a> <p>$1,500/mo 1 bd 1 ba</p>
</body></html>"""


def alert(sender="Zillow <instant-updates@mail.zillow.com>", msg_id="<a1@zillow>", subject="2 new rentals"):
    message = EmailMessage()
    message["From"], message["Subject"], message["Message-ID"] = sender, subject, msg_id
    message.set_content("See the HTML version.")
    message.add_alternative(ALERT, subtype="html")
    return message.as_bytes()


class FakeBox:
    """The few IMAP calls the source makes, over a list of raw messages."""

    def __init__(self, messages):
        self.messages, self.calls, self.readonly = messages, [], None

    def login(self, user, password):
        self.calls.append("login")

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]
        raw = self.messages[int(args[0]) - 1]
        if "HEADER" in args[1]:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"1 (BODY[] {1}", raw), b")"]

    def logout(self):
        self.calls.append("logout")


def model(calls=None):
    """Stand in for the local model: finds the two homes in the alert text."""
    def extract(settings, label, chunk):
        if calls is not None:
            calls.append(chunk)
        links = mail.LINK_RE.findall(chunk)
        return [
            ai.ExtractedListing(title="12 Pine St APT 3", address="12 Pine St APT 3, Lowell, MA 01852", town="Lowell",
                                price=1850, beds=2, baths=1, url=f"https://link.invalid/{links[0]}"),
            ai.ExtractedListing(title="9 Wharf St", address="9 Wharf St, Portland, ME 04101", town="Portland",
                                price=1500, beds=1, baths=1, url=f"https://link.invalid/{links[1]}"),
        ]
    return mock.patch.multiple(ai, status=lambda *a, **k: {"ok": True}, extract_listings=extract)


class MailTest(DbTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {mail.USER_ENV: "me@example.test", mail.PASSWORD_ENV: "app-password"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, box, now=NOW, **kw):
        return mail.fetch(self.settings, None, lambda _: None,
                          box=mail.ImapBox(self.settings["sources"]["mail"], connect=lambda: box), now=now, **kw)

    def test_alert_is_read_once_and_only_your_towns_are_kept(self):
        box, chunks = FakeBox([alert()]), []
        with model(chunks):
            result = self.fetch(box)
            again = self.fetch(FakeBox([alert()]))
        self.assertEqual([i["title"] for i in result.items], ["12 Pine St APT 3"])  # Portland is not on the list
        item = result.items[0]
        self.assertEqual((item["source"], item["town"], item["units"][0]["price_low"]), ("mail", "Lowell", 1850))
        self.assertEqual(item["url"], TRACKER + "1")       # the real link is put back
        self.assertNotIn("click.mail", chunks[0])          # the model never sees the long tracking links
        self.assertEqual(again.items, [])                  # already read: not sent to the model again
        self.assertEqual(len(chunks), 1)

    def test_mailbox_is_opened_read_only_and_bodies_are_peeked(self):
        box = FakeBox([alert()])
        with model():
            self.fetch(box)
        self.assertTrue(box.readonly)
        fetches = [c[2] for c in box.calls if isinstance(c, tuple) and c[0] == "fetch"]
        self.assertTrue(fetches and all("PEEK" in f for f in fetches))
        self.assertEqual(box.calls[-1], "logout")

    def test_gmail_token_reads_through_googles_api_and_leaves_the_token_file_alone(self):
        import base64, json, tempfile
        from pathlib import Path
        import httpx
        token = Path(tempfile.mkdtemp()) / "token.json"
        token.write_text(json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s",
                                     "token_uri": "https://oauth2.example.test/token"}))
        before, asked = token.read_text(), []

        def google(request: httpx.Request) -> httpx.Response:
            asked.append((request.method, request.url.path, dict(request.url.params)))
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "at"})
            assert request.method == "GET" and request.headers["authorization"] == "Bearer at"
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"messages": [{"id": "m1"}]})
            if request.url.params["format"] == "metadata":
                return httpx.Response(200, json={"payload": {"headers": [
                    {"name": "From", "value": "Zillow <instant-updates@mail.zillow.com>"}]}})
            return httpx.Response(200, json={"raw": base64.urlsafe_b64encode(alert()).decode().rstrip("=")})

        with model():
            result = mail.fetch(self.settings, None, lambda _: None, now=NOW,
                                box=mail.GmailBox(token, transport=httpx.MockTransport(google)))
        self.assertEqual([i["title"] for i in result.items], ["12 Pine St APT 3"])
        self.assertIn("zillow.com", asked[1][2]["q"])
        self.assertEqual(token.read_text(), before)

    def test_refused_gmail_token_says_so(self):
        import json, tempfile
        from pathlib import Path
        import httpx
        token = Path(tempfile.mkdtemp()) / "token.json"
        token.write_text(json.dumps({"refresh_token": "r"}))
        refuse = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
        with self.assertRaisesRegex(RuntimeError, "invalid_grant"):
            mail.GmailBox(token, transport=refuse)

    def test_other_senders_are_never_read(self):
        calls = []
        with model(calls):
            result = self.fetch(FakeBox([alert(sender="Bank <alerts@bank.example>"),
                                         alert(sender="x <a@notzillow.com>", msg_id="<b@x>")]))
        self.assertEqual((result.items, calls), ([], []))

    def test_no_sign_in_is_an_error_not_a_clean_hunt(self):
        with mock.patch.dict(os.environ, {mail.PASSWORD_ENV: ""}):
            with self.assertRaises(RuntimeError):
                mail.fetch(self.settings, None, lambda _: None, now=NOW)

    def test_unreachable_model_leaves_the_alert_for_next_time(self):
        with mock.patch.object(ai, "status", lambda *a, **k: {"ok": False}):
            with self.assertRaises(RuntimeError):
                self.fetch(FakeBox([alert()]))
        with model():
            self.assertEqual(len(self.fetch(FakeBox([alert()])).items), 1)

    def test_emailed_listing_stays_until_no_alert_mentions_it_for_keep_days(self):
        from renthunt import hunt
        with model():
            result = self.fetch(FakeBox([alert()]))
        with db.connect() as conn:
            listing_id, _ = hunt.upsert(conn, result.items[0], NOW.isoformat())
        quiet = self.fetch(FakeBox([]), now=NOW + timedelta(days=3))
        with db.connect() as conn:
            hunt._settle(conn, "mail", quiet, (NOW + timedelta(days=3)).isoformat(), 3)
        self.assertTrue(self.row(listing_id)["active"])    # quiet mail does not count against it
        self.fetch(FakeBox([]), now=NOW + timedelta(days=30))
        self.assertFalse(self.row(listing_id)["active"])

    def test_dry_run_remembers_nothing(self):
        with model():
            self.fetch(FakeBox([alert()]), store=False)
            self.assertEqual(len(self.fetch(FakeBox([alert()])).items), 1)
