# Home API Dashboard

A small local Flask app that watches a Nest Wifi / Google Wifi / OnHub router's
undocumented local API (`http://<router-ip>/api/v1/status`) and turns it into a
dashboard: live status, connectivity history, outage detection, a change log for
things like reboots and firmware updates, and an hourly internet speed test.

Everything runs locally — the only external calls are an occasional public-IP →
ISP lookup (cached) and the periodic speedtest itself.

## Features

- **Live router status**, reformatted from raw JSON into readable grouped
  sections (Network / Device), with badges for booleans and human-readable
  durations. A raw-JSON toggle is still there if you want it.
- **Router & network summary**: uptime since last reboot, current firmware
  version with an "update available" badge, and the public IP resolved to a
  hostname (reverse DNS) and ISP/city (via a cached lookup).
- **WAN uptime history**: a lightweight online/offline/unreachable timeline
  (plain CSS, not a chart) built from a background poll every
  `POLL_INTERVAL_SECONDS` (default 30s), persisted to SQLite.
- **Outage detection**: consecutive down-polls are grouped into discrete
  outage events (start, end, duration, type). Single missed polls are filtered
  out by default (`min_samples`) since those are usually noise, not real
  outages.
- **Notable changes log**: reboots (uptime counter reset), public IP changes
  (ISP DHCP lease renewal), Wi-Fi mesh channel changes, and firmware
  updates/availability — all derived from fields the router already reports.
- **Internet speed test**: a real download/upload test runs on a schedule
  (default hourly, aligned to the top of the hour) via `speedtest-cli`,
  pinned to explicit nearby servers (see note below), with a "run now" button
  and a bar chart bucketed by hour/day.
- **Search & export**: filter any of the history views by a time range, and
  export the raw history as CSV or JSON.
- **Router address discovery**: the "Router & network" panel shows the
  currently-configured router address with **Change** (manual entry) and
  **Auto-discover** buttons — no need to hand-edit `.env` or restart the app.
  Auto-discover tries your machine's likely gateway IP plus a short list of
  common home-router defaults, and only saves one that actually responds like
  this router's API. A change here is validated before saving and persists
  across restarts (stored in `history.db`, overriding the `.env` default).

## Run it

```bash
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\python.exe app.py
```

Open http://localhost:4200 (or whatever `PORT` is set to). On first run it'll
try to reach the router at `ROUTER_BASE_URL` (default `192.168.86.1`, a common
Nest Wifi/OnHub gateway address, though not a guarantee) — if that's wrong for
your network, use the **Change** or **Auto-discover** button in the "Router &
network" panel rather than editing `.env` and restarting.

## Configuration

Copy `.env.example` to `.env` and adjust as needed:

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `4200` | Port the Flask app listens on. |
| `ROUTER_BASE_URL` | `http://192.168.86.1` | Startup default for your router's LAN address — only used if nothing's been saved yet via the UI's Change/Auto-discover buttons, which take precedence once set. |
| `POLL_INTERVAL_SECONDS` | `30` | How often to poll the router's status API. |
| `SPEEDTEST_INTERVAL_SECONDS` | `3600` | How often to run a real speedtest. Each run saturates the connection for ~10-20s and uses real data. |
| `SPEEDTEST_SERVER_URLS` | (unset) | Comma-separated speedtest server upload URLs to pin to instead of the built-in defaults — see below. |

## Known quirks worth knowing about

- **Port exclusions on Windows.** Windows/Hyper-V/WSL periodically reserve
  dynamic TCP port ranges (`netsh interface ipv4 show excludedportrange
  protocol=tcp`) that can swallow whatever port you pick, causing the server
  to fail to bind with a permissions error even though nothing else is
  listening on it. If this happens, pick a different `PORT` in `.env`.
- **speedtest-cli's built-in server auto-discovery can be badly wrong.** It
  relies on speedtest.net's own IP geolocation, which for at least one real
  connection placed it ~1200km away from its actual location — and since the
  "closest server" shortlist is built from that same bad geolocation,
  `get_best_server()` never gets a chance to consider genuinely nearby
  servers. This app pins to explicit server URLs instead (two example
  Charlotte, NC servers by default). **You should replace these** with
  servers near your own location — search
  `https://www.speedtest.net/api/js/servers?engine=js&search=<your city>&limit=10`
  for candidates, or just delete the pin and let `get_best_server()` run
  unconstrained if it works fine for you. Override via `SPEEDTEST_SERVER_URLS`.
- **The router's local API is undocumented.** Field names and behavior come
  from observation, not an official spec — Google could change it at any
  time.

## Project layout

- `app.py` — Flask app: background pollers, SQLite storage, REST endpoints.
- `templates/index.html` — the whole frontend (vanilla JS + Chart.js via CDN,
  no build step).
- `history.db` — created on first run, not committed (see `.gitignore`).
