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
- **Watched pages.** Add any page that lists vacancies, such as a local property manager or a
  housing authority. Your local model reads it on each hunt and only re-reads it when the
  page changes. Pages that need JavaScript to show their listings will come up empty.
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
`RENTHUNT_DATA` (data folder).

## Adding another listing source

A source is a module in `renthunt/sources/` with a `fetch(settings, fetcher, log)` function
that returns a list of dicts (fields are described at the top of `sources/base.py`). Register
it in the `plan` inside `hunt.run()` and add an on/off entry under `sources` in `config.py`.

Zillow, Apartments.com and HotPads block automated requests, so they are not included.

## Layout

```
renthunt/app.py        web server, API, scheduler loop
renthunt/hunt.py       the hunt pipeline and schedule
renthunt/scoring.py    match rules and score
renthunt/chat.py       assistant and its tools
renthunt/ai.py         Ollama calls
renthunt/sources/      Craigslist, Rent.com, watched pages
renthunt/static/       the web interface
```
