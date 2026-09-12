import csv
import io
import logging
import os
import socket
import sqlite3
import sys
import threading
import time

import requests
import speedtest
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("home-api-dashboard")


def is_frozen():
    """True when running as a PyInstaller-bundled executable rather than from source."""
    return getattr(sys, "frozen", False)


def app_base_dir():
    """Where bundled resources (templates/) live: PyInstaller's extraction dir
    when frozen, otherwise this file's own directory."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir():
    """Where to persist history.db. When frozen, a bundled exe's own directory
    (or the PyInstaller temp extraction dir in --onefile mode) isn't a reliable
    place to write persistent data, so use a proper per-user data directory
    instead. Running from source keeps the original next-to-app.py behavior."""
    if not is_frozen():
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, "home-api-dashboard")
    os.makedirs(path, exist_ok=True)
    return path


app = Flask(
    __name__,
    template_folder=os.path.join(app_base_dir(), "templates"),
    static_folder=os.path.join(app_base_dir(), "static"),
    static_url_path="",
)
app.config["TEMPLATES_AUTO_RELOAD"] = not is_frozen()

def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        # Fail soft, not soft-crash-at-startup: a stray typo in .env
        # (POLL_INTERVAL_SECONDS=3o0, say) shouldn't take the whole app down
        # before it even gets to log anything useful.
        logger.warning("%s=%r isn't a valid integer; using the default (%s) instead.", name, raw, default)
        return default


ROUTER_BASE_URL = os.environ.get("ROUTER_BASE_URL", "http://192.168.86.1")

# Hard floors on the two configurable intervals, regardless of what .env says —
# this is a background process that runs unattended and indefinitely, so a
# typo or an over-eager edit (POLL_INTERVAL_SECONDS=1, say) shouldn't be able
# to turn it into something hammering the router or repeatedly saturating the
# household's internet connection. 10s still leaves plenty of headroom above
# what a router's local status API needs to handle comfortably; 300s (5 min)
# is already a lot more often than most people would want a ~10-20s speed
# test saturating their link for.
MIN_POLL_INTERVAL_SECONDS = 10
MIN_SPEEDTEST_INTERVAL_SECONDS = 300

_poll_interval_configured = _env_int("POLL_INTERVAL_SECONDS", 30)
POLL_INTERVAL_SECONDS = max(MIN_POLL_INTERVAL_SECONDS, _poll_interval_configured)
if POLL_INTERVAL_SECONDS != _poll_interval_configured:
    logger.warning(
        "POLL_INTERVAL_SECONDS=%s is below the %ss floor (protects the router from "
        "being polled too aggressively); using %ss instead.",
        _poll_interval_configured, MIN_POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS,
    )

_speedtest_interval_configured = _env_int("SPEEDTEST_INTERVAL_SECONDS", 3600)
SPEEDTEST_INTERVAL_SECONDS = max(MIN_SPEEDTEST_INTERVAL_SECONDS, _speedtest_interval_configured)
if SPEEDTEST_INTERVAL_SECONDS != _speedtest_interval_configured:
    logger.warning(
        "SPEEDTEST_INTERVAL_SECONDS=%s is below the %ss floor (each run saturates "
        "the connection for ~10-20s); using %ss instead.",
        _speedtest_interval_configured, MIN_SPEEDTEST_INTERVAL_SECONDS, SPEEDTEST_INTERVAL_SECONDS,
    )

DB_PATH = os.path.join(user_data_dir(), "history.db")

# A speedtest saturates the connection for ~10-20s — never let two run at once
# (a manual "run now" click racing the scheduled run would skew both results).
SPEEDTEST_LOCK = threading.Lock()

# Each discovery attempt fires off several probe requests (gateway guess + a
# handful of common router defaults). Nothing about that is heavy on its own,
# but a double-click, an impatient repeat click, or a stray script hitting the
# endpoint in a loop shouldn't be able to stack up overlapping bursts of them —
# same reasoning as SPEEDTEST_LOCK above.
DISCOVER_LOCK = threading.Lock()
SPEEDTEST_COLUMNS = ["ts", "success", "download_mbps", "upload_mbps", "ping_ms", "server_name", "error"]
SPEEDTEST_COLUMN_LIST_SQL = ", ".join(SPEEDTEST_COLUMNS)

# speedtest-cli's own "closest server" logic relies on speedtest.net's IP geolocation,
# which can be badly wrong (for this connection it placed us ~1200km away in Quebec,
# even though the DNS/ISP lookup elsewhere in this app correctly resolves to NC) —
# and get_best_server() only ever ranks among that (possibly wrong) shortlist. So we
# pin to explicit, verified-nearby servers instead of trusting the auto-discovery.
# Override with SPEEDTEST_SERVER_URLS (comma-separated) if these ever go stale.
_custom_server_urls = os.environ.get("SPEEDTEST_SERVER_URLS")
if _custom_server_urls:
    SPEEDTEST_SERVERS = [
        {"id": str(i), "sponsor": "custom", "name": "custom", "country": "", "url": u.strip()}
        for i, u in enumerate(_custom_server_urls.split(","))
        if u.strip()
    ]
else:
    SPEEDTEST_SERVERS = [
        {"id": "27833", "sponsor": "Uniti", "name": "Charlotte, NC", "country": "United States",
         "url": "http://charlotte02.speedtest.windstream.net:8080/speedtest/upload.php"},
        {"id": "46382", "sponsor": "RippleFiber", "name": "Charlotte, NC", "country": "United States",
         "url": "http://speedtest.ripplefiber.com:8080/speedtest/upload.php"},
    ]

COLUMNS = [
    "ts",
    "success",
    "latency_ms",
    "wan_online",
    "ethernet_link",
    "uptime_seconds",
    "mesh_channel",
    "public_ip",
    "software_version",
    "update_new_version",
    "update_status",
]
COLUMN_LIST_SQL = ", ".join(COLUMNS)

# Human-readable CSV column headers (the export is meant to be opened in a
# spreadsheet) — the raw names above stay as the SQL/JSON field names.
CSV_HEADERS = {
    "ts": "Timestamp",
    "success": "Poll Succeeded",
    "latency_ms": "Latency (ms)",
    "wan_online": "WAN Online",
    "ethernet_link": "Ethernet Link",
    "uptime_seconds": "Uptime (s)",
    "mesh_channel": "Mesh Channel",
    "public_ip": "Public IP",
    "software_version": "Software Version",
    "update_new_version": "Update Available Version",
    "update_status": "Update Status",
}

# Public-IP -> hostname/ISP lookups are cached in the isp_cache table (persists across
# restarts) with an in-memory layer on top (avoids a DB round trip on every 30s poll).
ISP_CACHE = {}


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Two background threads (poll_loop, speedtest_loop) plus request handlers can
    # all open writes around the same moment; without this, a write that lands
    # mid-transaction elsewhere fails immediately with "database is locked"
    # instead of just waiting briefly for the other one to finish.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            ts REAL NOT NULL,
            success INTEGER NOT NULL,
            latency_ms REAL,
            wan_online INTEGER,
            ethernet_link INTEGER,
            uptime_seconds INTEGER
        )
        """
    )
    # Additive migration for columns introduced after the table already existed.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(history)")}
    for col, col_type in (
        ("mesh_channel", "INTEGER"),
        ("public_ip", "TEXT"),
        ("software_version", "TEXT"),
        ("update_new_version", "TEXT"),
        ("update_status", "TEXT"),
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE history ADD COLUMN {col} {col_type}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS isp_cache (
            ip TEXT PRIMARY KEY,
            hostname TEXT,
            isp TEXT,
            city TEXT,
            region TEXT,
            country TEXT,
            looked_up_at REAL
        )
        """
    )
    isp_cache_columns = {row["name"] for row in conn.execute("PRAGMA table_info(isp_cache)")}
    for col, col_type in (("latitude", "REAL"), ("longitude", "REAL")):
        if col not in isp_cache_columns:
            conn.execute(f"ALTER TABLE isp_cache ADD COLUMN {col} {col_type}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS speedtest_history (
            ts REAL NOT NULL,
            success INTEGER NOT NULL,
            download_mbps REAL,
            upload_mbps REAL,
            ping_ms REAL,
            server_name TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()

    # A router URL saved via the UI (manual entry or auto-discovery) overrides the
    # .env default and persists across restarts.
    global ROUTER_BASE_URL
    row = conn.execute("SELECT value FROM app_config WHERE key = 'router_base_url'").fetchone()
    if row and row["value"]:
        ROUTER_BASE_URL = row["value"]
    conn.close()


def poll_once():
    start = time.monotonic()
    try:
        r = requests.get(f"{ROUTER_BASE_URL}/api/v1/status", timeout=5)
        latency_ms = (time.monotonic() - start) * 1000
        data = r.json()
        wan = data.get("wan", {})
        system = data.get("system", {})
        mesh = data.get("meshInfo", {})
        software = data.get("software", {})
        row = (
            time.time(),
            1,
            latency_ms,
            1 if wan.get("online") else 0,
            1 if wan.get("ethernetLink") else 0,
            system.get("uptime"),
            mesh.get("mesh-channel"),
            wan.get("localIpAddress"),  # despite the name, this is the WAN-facing (public) IP
            software.get("softwareVersion"),
            software.get("updateNewVersion"),
            software.get("updateStatus"),
        )
    except (requests.RequestException, ValueError):
        row = (time.time(), 0, None, None, None, None, None, None, None, None, None)

    conn = get_db()
    placeholders = ", ".join("?" for _ in COLUMNS)
    conn.execute(f"INSERT INTO history ({COLUMN_LIST_SQL}) VALUES ({placeholders})", row)
    conn.commit()
    conn.close()


def poll_loop():
    while True:
        # Skip this cycle if a speedtest is actively saturating the link — the
        # router's own local API can briefly stop responding while its CPU is
        # busy routing that traffic (confirmed: a router-unreachable blip lined
        # up almost exactly with a speedtest run). That's an expected side
        # effect of testing at all, not a real problem worth logging as an
        # "outage" — logging it anyway would just be false-positive noise.
        try:
            if not SPEEDTEST_LOCK.locked():
                poll_once()
        except Exception:
            # This is a daemon thread with nothing else watching it — an
            # uncaught exception here (e.g. a transient DB or disk error)
            # would otherwise kill polling silently and permanently, for as
            # long as the app stays running. Log it and try again next cycle.
            logger.exception("poll_once() failed unexpectedly; will retry next cycle")
        time.sleep(POLL_INTERVAL_SECONDS)


def run_speedtest_once():
    """Run one real speedtest (saturates the link for ~10-20s) and log the result.
    Returns the result dict whether it succeeded or not."""
    if not SPEEDTEST_LOCK.acquire(blocking=False):
        return {"error": "a speedtest is already running"}

    try:
        row = {"ts": time.time(), "success": 0, "download_mbps": None, "upload_mbps": None,
               "ping_ms": None, "server_name": None, "error": None}
        try:
            st = speedtest.Speedtest()
            st.get_best_server(servers=SPEEDTEST_SERVERS)
            # Tried reducing thread count (1, then 3, vs. speedtest-cli's default 8)
            # to lighten the router's load — both wrecked accuracy far more than
            # proportionally (3 threads measured ~17 Mbps on a 200+ Mbps connection,
            # not the ~90 Mbps a linear scale-down would predict), so there's no
            # useful middle ground here: either the reading is trustworthy or it
            # isn't. Left at the library default; see poll_loop() for the actual
            # fix (skip logging a router poll while a speedtest is in flight,
            # rather than trying to make the speedtest itself lighter).
            download_bps = st.download()
            upload_bps = st.upload()
            results = st.results.dict()
            row.update({
                "success": 1,
                "download_mbps": download_bps / 1e6,
                "upload_mbps": upload_bps / 1e6,
                "ping_ms": results.get("ping"),
                "server_name": f"{results['server']['sponsor']} - {results['server']['name']}",
            })
        except Exception as err:  # speedtest-cli raises its own exception types plus can hit network errors
            row["error"] = str(err)

        conn = get_db()
        placeholders = ", ".join("?" for _ in SPEEDTEST_COLUMNS)
        conn.execute(
            f"INSERT INTO speedtest_history ({SPEEDTEST_COLUMN_LIST_SQL}) VALUES ({placeholders})",
            tuple(row[c] for c in SPEEDTEST_COLUMNS),
        )
        conn.commit()
        conn.close()
        return row
    finally:
        SPEEDTEST_LOCK.release()


def speedtest_loop():
    # Run once immediately so there's data right away, then align every subsequent
    # run to the next wall-clock boundary of the interval — with the default 3600s
    # that means the top of the hour (epoch is UTC-based, and US timezones sit at
    # whole-hour offsets from UTC, so this lines up with local top-of-hour too).
    # +1s buffer: time.sleep() commonly wakes a fraction of a second early, and
    # landing at e.g. 11:59:59 instead of 12:00:00 would bucket the run into the
    # wrong hour on the chart (bucketing floors to the hour the timestamp falls in).
    try:
        run_speedtest_once()
    except Exception:
        logger.exception("initial speedtest run failed unexpectedly")
    while True:
        sleep_seconds = SPEEDTEST_INTERVAL_SECONDS - (time.time() % SPEEDTEST_INTERVAL_SECONDS) + 1
        time.sleep(sleep_seconds)
        try:
            run_speedtest_once()
        except Exception:
            # Same reasoning as poll_loop(): don't let one bad run (e.g. a DB
            # write error after the speedtest itself succeeded) silently end
            # all future scheduled speedtests for the rest of the app's uptime.
            logger.exception("run_speedtest_once() failed unexpectedly; will retry next cycle")


def looks_like_router(url):
    """GET <url>/api/v1/status and check the response has this router API's
    expected shape, to avoid saving a URL that isn't actually our router."""
    try:
        r = requests.get(f"{url}/api/v1/status", timeout=2)
        if not r.ok:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if "wan" not in data or "system" not in data:
            return False, "responded, but not with the expected router API shape"
        return True, None
    except requests.RequestException as err:
        return False, str(err)
    except ValueError:
        return False, "response wasn't valid JSON"


def set_router_base_url(url):
    url = url.rstrip("/")
    ok, err = looks_like_router(url)
    if not ok:
        return False, err

    global ROUTER_BASE_URL
    ROUTER_BASE_URL = url
    conn = get_db()
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES ('router_base_url', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (url,),
    )
    conn.commit()
    conn.close()
    return True, None


def guess_local_gateway():
    """Best-effort guess at the LAN gateway IP: open a UDP socket toward a public
    address (no packet actually sent for UDP) to learn which local IP the OS would
    route through, then assume the gateway is that subnet's .1 — true for the
    overwhelming majority of home routers, though not guaranteed."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()
        return local_ip.rsplit(".", 1)[0] + ".1"
    except OSError:
        return None


def discover_router_base_url():
    """Try the likely gateway IP plus a short list of common home-router defaults,
    in order, and use the first one that actually responds like this router's API."""
    candidates = []
    guessed = guess_local_gateway()
    if guessed:
        candidates.append(guessed)
    for common in ("192.168.86.1", "192.168.1.1", "192.168.0.1", "10.0.0.1"):
        if common not in candidates:
            candidates.append(common)

    tried = []
    for ip in candidates:
        url = f"http://{ip}"
        ok, err = looks_like_router(url)
        tried.append({"url": url, "ok": ok, "error": err})
        if ok:
            return url, tried
    return None, tried


@app.get("/")
def index():
    return render_template("index.html", poll_interval=POLL_INTERVAL_SECONDS)


@app.get("/api/router/status")
def router_status():
    try:
        r = requests.get(f"{ROUTER_BASE_URL}/api/v1/status", timeout=5)
        return jsonify(r.json()), r.status_code
    except requests.RequestException as err:
        return jsonify({"error": "could not reach router", "detail": str(err)}), 502


@app.get("/api/config/router")
def router_config_get():
    return jsonify({"router_base_url": ROUTER_BASE_URL})


@app.post("/api/config/router")
def router_config_set():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "no URL given"}), 400
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"http://{url}"

    ok, err = set_router_base_url(url)
    if not ok:
        return jsonify({"success": False, "error": err}), 502
    return jsonify({"success": True, "router_base_url": ROUTER_BASE_URL})


@app.post("/api/config/router/discover")
def router_config_discover():
    if not DISCOVER_LOCK.acquire(blocking=False):
        return jsonify({"success": False, "error": "a discovery attempt is already running"}), 409
    try:
        url, tried = discover_router_base_url()
    finally:
        DISCOVER_LOCK.release()

    if url is None:
        return jsonify({"success": False, "tried": tried}), 502

    ok, err = set_router_base_url(url)
    if not ok:
        return jsonify({"success": False, "tried": tried, "error": err}), 502
    return jsonify({"success": True, "router_base_url": ROUTER_BASE_URL, "tried": tried})


def parse_time_range():
    """Read optional ?start=&end= query params (unix seconds) and build a WHERE clause."""
    clauses = []
    params = []
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params, (start is not None or end is not None)


@app.get("/api/router/history")
def router_history():
    where, params, has_range = parse_time_range()
    # No explicit range: just the most recent samples for the live charts.
    # An explicit range (from the search UI): return everything in it, up to a sane cap.
    limit = 5000 if has_range else 500
    conn = get_db()
    rows = conn.execute(
        f"SELECT {COLUMN_LIST_SQL} FROM history {where} ORDER BY ts DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    points = [dict(row) for row in reversed(rows)]
    return jsonify(points)


def row_status(row):
    """Classify a history row: 'online', 'offline' (WAN down but router reachable), or 'unreachable' (poll failed)."""
    if not row["success"]:
        return "unreachable"
    return "online" if row["wan_online"] else "offline"


def update_available(row):
    """The router reports an available version in updateNewVersion; '0.0.0.0' (or blank) means none."""
    new_version = row["update_new_version"]
    if not new_version or new_version == "0.0.0.0":
        return False
    return new_version != row["software_version"]


@app.get("/api/router/current")
def router_current():
    conn = get_db()
    row = conn.execute(
        f"SELECT {COLUMN_LIST_SQL} FROM history ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"status": "no_data"})
    data = dict(row)
    data["status"] = row_status(row)
    data["update_available"] = update_available(row)
    return jsonify(data)


@app.get("/api/router/outages")
def router_outages():
    where, params, _ = parse_time_range()
    limit = 20000
    conn = get_db()
    rows = conn.execute(
        f"SELECT {COLUMN_LIST_SQL} FROM history {where} ORDER BY ts ASC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()

    outages = []
    current = None
    for row in rows:
        down = row_status(row) != "online"
        if down:
            if current is None:
                current = {"start": row["ts"], "end": row["ts"], "kind": row_status(row), "samples": 1}
            else:
                current["end"] = row["ts"]
                current["samples"] += 1
                if row_status(row) != current["kind"]:
                    current["kind"] = "mixed"
        else:
            if current is not None:
                outages.append(current)
                current = None
    ongoing = current is not None
    if current is not None:
        outages.append(current)

    for o in outages:
        o["duration_seconds"] = max(o["end"] - o["start"], 0)
    outages.sort(key=lambda o: o["start"], reverse=True)
    if outages and ongoing:
        outages[0]["ongoing"] = True

    # A single missed poll (duration 0, one sample) is usually noise — a momentary
    # timeout, or (during development) the dashboard's own server restarting —
    # not a real outage. Require at least min_samples consecutive down polls,
    # except for an outage that's still ongoing right now (always worth showing).
    min_samples = request.args.get("min_samples", default=2, type=int)
    significant = [o for o in outages if o["samples"] >= min_samples or o.get("ongoing")]
    brief_count = len(outages) - len(significant)

    return jsonify({"outages": significant, "brief_count": brief_count, "min_samples": min_samples})


@app.get("/api/router/events")
def router_events():
    """Notable changes detected between consecutive polls: reboots, public IP changes,
    mesh channel changes, and firmware updates — all derived from fields the router
    already reports but that the raw status snapshot doesn't call attention to."""
    where, params, _ = parse_time_range()
    limit = 20000
    conn = get_db()
    rows = conn.execute(
        f"SELECT {COLUMN_LIST_SQL} FROM history {where} ORDER BY ts ASC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()

    events = []
    prev = None
    for row in rows:
        if not row["success"]:
            continue
        if prev is not None:
            if (
                row["uptime_seconds"] is not None
                and prev["uptime_seconds"] is not None
                and row["uptime_seconds"] < prev["uptime_seconds"]
            ):
                events.append({"ts": row["ts"], "type": "reboot", "detail": "Router uptime reset — it rebooted"})
            if (
                row["public_ip"]
                and prev["public_ip"]
                and row["public_ip"] != prev["public_ip"]
            ):
                events.append({
                    "ts": row["ts"], "type": "public_ip_change",
                    "detail": f"Public IP changed from {prev['public_ip']} to {row['public_ip']}",
                })
            if (
                row["mesh_channel"] is not None
                and prev["mesh_channel"] is not None
                and row["mesh_channel"] != prev["mesh_channel"]
            ):
                events.append({
                    "ts": row["ts"], "type": "mesh_channel_change",
                    "detail": f"Mesh Wi-Fi channel changed from {prev['mesh_channel']} to {row['mesh_channel']}",
                })
            if (
                row["software_version"]
                and prev["software_version"]
                and row["software_version"] != prev["software_version"]
            ):
                events.append({
                    "ts": row["ts"], "type": "firmware_update",
                    "detail": f"Firmware updated from {prev['software_version']} to {row['software_version']}",
                })
            if update_available(row) and not update_available(prev):
                events.append({
                    "ts": row["ts"], "type": "update_available",
                    "detail": f"Firmware update became available: {row['update_new_version']}",
                })
        prev = row

    events.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify(events)


def resolve_hostname(ip):
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(3)
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        # Covers socket.herror/gaierror/timeout (all OSError subclasses) plus
        # anything else the platform's resolver can raise for a malformed or
        # unresolvable address — this is reachable with a user-supplied ?ip=,
        # so it shouldn't be able to 500 the request.
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


# Downdetector has no public API — these are just deep links to its per-company
# status pages (verified to actually resolve, not guessed), plus each provider's
# own official outage-check page where one could be confirmed. Matching is a
# simple case-insensitive substring check against whatever ipapi.co's "org"
# field returns, which varies in exact wording, so keep keywords broad but not
# so broad they'd false-positive-match an unrelated company name.
ISP_PROVIDERS = [
    (["spectrum", "charter"], "Spectrum", "spectrum", "https://www.spectrum.net/outage-map"),
    (["comcast", "xfinity"], "Xfinity", "xfinity", "https://www.xfinity.com/support/articles/check-service-outage"),
    (["at&t"], "AT&T", "att", "https://www.att.com/outages/"),
    (["verizon"], "Verizon", "verizon", "https://www.verizon.com/support/residential/service-outage"),
    (["t-mobile", "tmobile"], "T-Mobile", "t-mobile", "https://www.t-mobile.com/support/coverage/network-outages"),
    (["cox communications"], "Cox", "cox-communications", "https://www.cox.com/residential/support/outages.html"),
    (["centurylink", "lumen"], "CenturyLink", "centurylink", "https://www.centurylink.com/home/help/internet/internet-or-phone-not-working.html"),
    (["windstream", "kinetic"], "Windstream (Kinetic)", "windstream", None),
]


def match_isp_provider(isp_name):
    if not isp_name:
        return None
    lowered = isp_name.lower()
    for keywords, display_name, slug, official_url in ISP_PROVIDERS:
        if any(kw in lowered for kw in keywords):
            return {
                "matched_provider": display_name,
                "downdetector_url": f"https://downdetector.com/status/{slug}/",
                "official_outage_url": official_url,
            }
    return None


@app.get("/api/router/isp")
def router_isp():
    ip = request.args.get("ip")
    if not ip:
        conn = get_db()
        row = conn.execute(
            "SELECT public_ip FROM history WHERE public_ip IS NOT NULL ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        ip = row["public_ip"] if row else None
    if not ip:
        return jsonify({"error": "no public IP known yet"}), 404

    cached = ISP_CACHE.get(ip)
    if cached is None:
        conn = get_db()
        row = conn.execute(
            "SELECT ip, hostname, isp, city, region, country, latitude, longitude "
            "FROM isp_cache WHERE ip = ?", (ip,)
        ).fetchone()
        conn.close()
        if row:
            cached = dict(row)
            ISP_CACHE[ip] = cached

    # A cache entry from before latitude/longitude was tracked has those as NULL —
    # treat that as incomplete rather than serving a permanently mapless response.
    if cached is not None and cached.get("latitude") is not None:
        return jsonify({**cached, **(match_isp_provider(cached["isp"]) or {})})

    result = {
        "ip": ip, "hostname": resolve_hostname(ip), "isp": None,
        "city": None, "region": None, "country": None,
        "latitude": None, "longitude": None,
    }
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=5, headers={"User-Agent": "home-api-dashboard/1.0"})
        if r.ok:
            j = r.json()
            if not j.get("error"):
                result["isp"] = j.get("org")
                result["city"] = j.get("city")
                result["region"] = j.get("region")
                result["country"] = j.get("country_name")
                result["latitude"] = j.get("latitude")
                result["longitude"] = j.get("longitude")
    except requests.RequestException:
        pass

    # Only cache a successful lookup — caching a failure (e.g. ipapi.co timed out
    # or rate-limited us) would otherwise pin that IP to "unknown ISP" forever.
    # A public IP essentially never changes for a home connection, so persisting
    # this to the DB means it survives restarts instead of re-fetching every time.
    if result["isp"] is not None:
        ISP_CACHE[ip] = result
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO isp_cache "
            "(ip, hostname, isp, city, region, country, latitude, longitude, looked_up_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (result["ip"], result["hostname"], result["isp"], result["city"], result["region"],
             result["country"], result["latitude"], result["longitude"], time.time()),
        )
        conn.commit()
        conn.close()
    return jsonify({**result, **(match_isp_provider(result["isp"]) or {})})


@app.get("/api/speedtest/current")
def speedtest_current():
    conn = get_db()
    row = conn.execute(
        f"SELECT {SPEEDTEST_COLUMN_LIST_SQL} FROM speedtest_history ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"status": "no_data"})
    return jsonify(dict(row))


@app.get("/api/speedtest/history")
def speedtest_history():
    where, params, has_range = parse_time_range()
    limit = 5000 if has_range else 500
    conn = get_db()
    rows = conn.execute(
        f"SELECT {SPEEDTEST_COLUMN_LIST_SQL} FROM speedtest_history {where} ORDER BY ts DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in reversed(rows)])


@app.post("/api/speedtest/run")
def speedtest_run():
    result = run_speedtest_once()
    if result.get("error") == "a speedtest is already running":
        return jsonify(result), 409
    if not result.get("success"):
        return jsonify(result), 502
    return jsonify(result)


@app.get("/api/router/export")
def router_export():
    fmt = request.args.get("format", "csv").lower()
    where, params, _ = parse_time_range()
    conn = get_db()
    rows = conn.execute(
        f"SELECT {COLUMN_LIST_SQL} FROM history {where} ORDER BY ts ASC",
        params,
    ).fetchall()
    conn.close()

    stamp = time.strftime("%Y%m%d-%H%M%S")

    if fmt == "json":
        body = jsonify([dict(row) for row in rows])
        body.headers["Content-Disposition"] = f"attachment; filename=router-history-{stamp}.json"
        return body

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([CSV_HEADERS[c] for c in COLUMNS])
    for row in rows:
        values = []
        for c in COLUMNS:
            v = row[c]
            if c == "ts" and v is not None:
                v = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
            values.append(v)
        writer.writerow(values)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=router-history-{stamp}.csv"},
    )


if __name__ == "__main__":
    init_db()
    logger.info(
        "Starting background pollers (router every %ss, speedtest every %ss); router at %s",
        POLL_INTERVAL_SECONDS, SPEEDTEST_INTERVAL_SECONDS, ROUTER_BASE_URL,
    )
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=speedtest_loop, daemon=True).start()
    port = _env_int("PORT", 4200)
    logger.info("Listening on http://0.0.0.0:%s", port)
    # threaded=True so the several fetches the dashboard fires per refresh can be
    # served concurrently instead of queueing behind each other one at a time.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
