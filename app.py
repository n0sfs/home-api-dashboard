import csv
import io
import os
import socket
import sqlite3
import threading
import time

import requests
import speedtest
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

ROUTER_BASE_URL = os.environ.get("ROUTER_BASE_URL", "http://192.168.86.1")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 30))
SPEEDTEST_INTERVAL_SECONDS = int(os.environ.get("SPEEDTEST_INTERVAL_SECONDS", 3600))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")

# A speedtest saturates the connection for ~10-20s — never let two run at once
# (a manual "run now" click racing the scheduled run would skew both results).
SPEEDTEST_LOCK = threading.Lock()
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

# Public-IP -> hostname/ISP lookups are cached in the isp_cache table (persists across
# restarts) with an in-memory layer on top (avoids a DB round trip on every 30s poll).
ISP_CACHE = {}


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
        poll_once()
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
    run_speedtest_once()
    while True:
        sleep_seconds = SPEEDTEST_INTERVAL_SECONDS - (time.time() % SPEEDTEST_INTERVAL_SECONDS) + 1
        time.sleep(sleep_seconds)
        run_speedtest_once()


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
    url, tried = discover_router_base_url()
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
    except (socket.herror, socket.gaierror, socket.timeout):
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


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

    if ip in ISP_CACHE:
        return jsonify(ISP_CACHE[ip])

    conn = get_db()
    cached = conn.execute(
        "SELECT ip, hostname, isp, city, region, country FROM isp_cache WHERE ip = ?", (ip,)
    ).fetchone()
    if cached:
        conn.close()
        result = dict(cached)
        ISP_CACHE[ip] = result
        return jsonify(result)

    result = {"ip": ip, "hostname": resolve_hostname(ip), "isp": None, "city": None, "region": None, "country": None}
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=5, headers={"User-Agent": "home-api-dashboard/1.0"})
        if r.ok:
            j = r.json()
            if not j.get("error"):
                result["isp"] = j.get("org")
                result["city"] = j.get("city")
                result["region"] = j.get("region")
                result["country"] = j.get("country_name")
    except requests.RequestException:
        pass

    # Only cache a successful lookup — caching a failure (e.g. ipapi.co timed out
    # or rate-limited us) would otherwise pin that IP to "unknown ISP" forever.
    # A public IP essentially never changes for a home connection, so persisting
    # this to the DB means it survives restarts instead of re-fetching every time.
    if result["isp"] is not None:
        ISP_CACHE[ip] = result
        conn.execute(
            "INSERT OR REPLACE INTO isp_cache (ip, hostname, isp, city, region, country, looked_up_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (result["ip"], result["hostname"], result["isp"], result["city"], result["region"], result["country"], time.time()),
        )
        conn.commit()
    conn.close()
    return jsonify(result)


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
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([row[c] for c in COLUMNS])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=router-history-{stamp}.csv"},
    )


if __name__ == "__main__":
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=speedtest_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 4200))
    app.run(host="0.0.0.0", port=port, debug=False)
