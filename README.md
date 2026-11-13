# RentHunt

by Draygen aka Brian Wallace

A local web app that hunts for rental vacancies in the Merrimack Valley (Lowell, Dracut,
Chelmsford, Lawrence, Haverhill, Methuen, Tewksbury, Billerica, plus any town you add),
scores them against what you want, and re-checks on a schedule. An assistant powered by
your local Ollama models lets you search and steer it in plain words.

Everything stays on this machine: listings in `data/renthunt.db`, the model in Ollama.

## Run it

```bash
./renthunt.sh                  # start the app, then open http://127.0.0.1:8642
./renthunt.sh install-service  # keep it running in the background (systemd user service)
./renthunt.sh hunt             # run one hunt from the terminal
./renthunt.sh status | uninstall-service
```

Scheduled hunts happen while the app is running, so use `install-service` for hands-off
operation. Under WSL the service runs whenever WSL itself is up.

## Sharing it on a public address

On this machine the app is fully open. Anyone who reaches it through a tunnel or proxy is a
visitor: they can browse listings and the map, while the assistant, settings, hunts, notes and
tracking need the admin password. The password is generated on first run in
`data/admin_password` (or set `RENTHUNT_PASSWORD`).

```bash
./renthunt.sh tunnel   # public address through a Cloudflare quick tunnel (needs cloudflared)
```

A quick tunnel gets a new random `trycloudflare.com` address each time it starts.

## What it does

- **Sources.** Craigslist (one small-radius search per town, then the full post for new
  matches) and Rent.com (apartment communities with floor plans and availability). Requests
  are spaced a few seconds apart; a full hunt is about 40 page loads.
- **RentCast (optional).** A paid listings API with a small free tier. Off by default; see
  "RentCast" below.
- **Email alerts (optional).** Zillow, Apartments.com, HotPads and Realtor.com block automated
  visits but will email you a saved search. RentHunt reads those alert emails (read-only) and
  your local model pulls the listings out. Off by default; see "Email alerts" below.
- **Watched pages.** Add any page that lists vacancies, such as a local property manager or a
  housing authority. AppFolio listing pages (`<company>.appfolio.com/listings`, which many
  local managers use), RentPress search pages (Princeton Properties'
  `princetonproperties.com/search/`) and pages with schema.org JSON-LD are read directly; anything else is
  read by your local model, and only re-read when the page changes. Settings shows how each
  page's last read went (read OK, no vacancies, blocked, came back empty, not read) and when
  it was last read successfully. A page that could not be read keeps its listings, marked
  "Availability unverified". Pages that need JavaScript can be loaded in a headless browser;
  see "Optional browser rendering".
- **Duplicates.** The same home on two sources is shown once when the street address, unit
  and town agree (or, with no unit given, bedrooms and rent as well). Different units in a
  building are never merged. Each copy keeps its own record and link ("Also listed" in the
  detail view), and your favorites, notes and tracking stay with the listing.
- **Missing listings.** A listing only counts as missing when the search or page it came
  from was actually checked. A hunt where some searches failed is reported as partly
  checked, with the list of what failed.
- **Scoring.** Each listing gets a 0-100 match score from rent, town order, nice-to-have
  features, freshness, and detail. Hard rules (rent, bedrooms, towns, pets, must-have and
  excluded words) decide whether it is a match at all.
- **Warnings.** Suspicious wording or a rent far below the local median is flagged as a
  possible scam and pushed down the list. Reposts of the same Craigslist ad are folded together.
- **Tracking.** Save favorites, hide listings, record where each one stands (contacted,
  touring, applied), and keep notes. Rent changes are recorded.
- **Alerts.** New matches above a score you choose show as NEW in the app and can be pushed
  to an ntfy.sh topic or a Discord/Slack webhook.
- **Assistant.** Ask "what's new since yesterday?", say "I need two bedrooms under $2,100
  that take cats", or ask for a draft inquiry message. It can change your criteria, towns and
  schedule, mark listings, start a hunt, and add watched pages.

## Customizing

Everything is in **Settings** in the app: towns, rent and bedroom range, pets, must-have /
nice-to-have / excluded words, sources, schedule and quiet hours, alerts, and which Ollama
model the assistant uses. Defaults live in `renthunt/config.py`.

Environment variables: `RENTHUNT_PORT` (default 8642), `RENTHUNT_HOST` (default 127.0.0.1),
`RENTHUNT_DATA` (data folder), `RENTCAST_API_KEY`, `RENTHUNT_MAIL_USER`, `RENTHUNT_MAIL_PASSWORD`. A `.env` file in the project folder is
read at start-up.

## Watching a property manager's page

Find the page that actually lists the units, not the marketing page that links to it. For
Avise Properties that is `https://californiapm.appfolio.com/listings`. Try a page before
adding it (nothing is stored):

```bash
./renthunt.sh check-page https://californiapm.appfolio.com/listings
./renthunt.sh check-page https://example.com/vacancies --browser
```

Only listings in towns on your town list are kept, so add the towns first (Avise is in New
Hampshire: Hudson 03051, Nashua 03060 and so on).

## Optional browser rendering

Only needed for watched pages that build their listings with JavaScript ("Came back empty").

```bash
uv pip install --python .venv/bin/python playwright     # or: .venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
# if Chromium will not start for want of system libraries:
sudo .venv/bin/python -m playwright install-deps chromium
```

Restart RentHunt, then tick "Use browser" on the page in Settings. Without Playwright
everything else works as before. The browser is plain headless Chromium: it does not get
past CAPTCHAs or bot checks, and such pages are reported as blocked.

## Email alerts

1. On each site you want (Zillow, Apartments.com, HotPads, Realtor.com, Zumper...), save a
   rental search for your towns and turn on email alerts to the mailbox RentHunt will read.
2. Give RentHunt a way to read that mailbox. Either:
   - **Google read-only token** (Gmail; nothing to type): put the path of a Google OAuth token
     file with the `gmail.readonly` scope in `sources.mail.gmail_token_file` (stored settings
     or `config.py`). The token the career-agent project keeps in `data/google_token.json`
     works as it is. RentHunt only reads the file, and the token cannot change mail.
   - **IMAP**: `echo 'RENTHUNT_MAIL_USER=you@gmail.com' >> .env` and
     `echo 'RENTHUNT_MAIL_PASSWORD=your-app-password' >> .env` (for Gmail, an app password
     from https://myaccount.google.com/apppasswords), then `chmod 600 .env`.
3. Restart RentHunt, try it with `./renthunt.sh check-mail` (prints what it would find and
   stores nothing), then switch on "Email alerts" in Settings → Sources.

Only mail from the senders listed in Settings is opened, each alert is read once, and nothing
in the mailbox is marked, moved or deleted. An emailed listing cannot be re-checked, so it is
kept until no alert has mentioned it for 21 days ("Keep a listing for"). Each alert costs one
or more local-model calls; at most 15 are read per hunt.

## RentCast

1. Create an API key at https://app.rentcast.io/app/api.
2. Put it where RentHunt can read it, never in Settings or git (`.env` is git-ignored):

   ```bash
   echo 'RENTCAST_API_KEY=your-key-here' >> .env && chmod 600 .env
   ```

3. Restart RentHunt, then switch on RentCast in Settings → Sources.

Every request is billed against your RentCast plan, so three limits apply (Settings →
Sources): requests per hunt (default 8), requests per billing month (default 45, a hard
stop; the free plan allows 50), and hours between checks of the same town (default 168). One
check of one town is one request, or more if it has over 500 results. Towns checked longest
ago go first. With 8 towns the defaults use about 32 requests a month. Set `billing_day` in
`config.py` defaults or the stored settings if your plan does not renew on the 1st.

RentCast gives no web page for a listing, so "Open listing" goes to the agent's site when
RentCast supplies one and otherwise to a map of the address.

## Tests

```bash
./renthunt.sh test      # offline; uses fixtures in tests/fixtures and a throwaway database
```

## Adding another listing source

A source is a module in `renthunt/sources/` with a `fetch(settings, fetcher, log)` function
that returns a `SourceResult` (or simply a list of dicts; both are described at the top of
`sources/base.py`). Register
it in the `plan` inside `hunt.run()` and add an on/off entry under `sources` in `config.py`.

Zillow, Apartments.com, HotPads, Zumper and PadMapper block automated requests, so they are
not read directly; their saved-search emails can be (see "Email alerts").

## Layout

```
renthunt/app.py        web server, API, scheduler loop
renthunt/hunt.py       the hunt pipeline and schedule
renthunt/scoring.py    match rules and score
renthunt/chat.py       assistant and its tools
renthunt/ai.py         Ollama calls
renthunt/dedupe.py     matching the same home across sources
renthunt/sources/      Craigslist, Rent.com, RentCast, email alerts, watched pages (+ structured data, browser)
tests/                 offline tests and fixtures
renthunt/static/       the web interface
```
