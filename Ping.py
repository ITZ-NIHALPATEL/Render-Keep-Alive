"""
Render Keep-Alive Pinger
~~~~~~~~~~~~~~~~~~~~~~~~
Pings ALL sites from sites.json concurrently every 10 minutes.
Sends Telegram alerts when a site is confirmed down after retries.

Deploy this on Render as a free web service — the background thread
pings all your other services (and itself) every 10 min, so nothing
ever spins down.

Environment variables:
  PORT              – Web server port         (Render sets this automatically)
  SELF_URL          – This service's own URL   (set to your Render URL)
  TELEGRAM_TOKEN    – Bot token for alerts     (optional)
  TELEGRAM_CHAT_ID  – Chat ID for alerts       (optional)
"""

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Force unbuffered output so Render logs appear in real time
sys.stdout.reconfigure(line_buffering=True)

# ──────────────────────── Load .env ─────────────────────────── #

def load_env():
    """Load variables from .env file into os.environ (stdlib only)."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:       # don't override real env vars
                os.environ[key] = value

load_env()

# ─────────────────────────── Config ─────────────────────────── #

SITES_FILE     = os.path.join(os.path.dirname(__file__), "sites.json")
PING_INTERVAL  = 600          # 10 minutes
REQUEST_TIMEOUT = 10          # per-request timeout (seconds)
MAX_RETRIES    = 2            # extra attempts after first failure
RETRY_DELAY    = 5            # seconds between retries

PORT           = int(os.environ.get("PORT", 8080))
SELF_URL       = os.environ.get("SELF_URL", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─────────────────────── Global State ───────────────────────── #

last_results = []             # latest ping cycle results
cycle_count  = 0              # total completed cycles

# ─────────────────────────── Helpers ────────────────────────── #

def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_sites():
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        sites = json.load(f)
    if not isinstance(sites, list) or not sites:
        raise ValueError("sites.json must be a non-empty JSON array")

    # Add self URL if set (keeps THIS service alive on Render)
    if SELF_URL and SELF_URL not in sites:
        sites.append(SELF_URL)

    return sites


def send_telegram_alert(site, status):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    text = (
        "🚨 <b>UPTIME ALERT</b> 🚨\n\n"
        f"<b>Site:</b> {site}\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Time:</b> {ts()}\n\n"
        "Please check your system!"
    )

    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode()

    req = Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        urlopen(req, timeout=10)
        print(f"  📢 Telegram alert sent for {site}")
    except Exception as e:
        print(f"  📢 Telegram alert failed: {e}")


# ──────────────────────────── Ping ──────────────────────────── #

def ping(url):
    """
    Ping a single URL. Tries GET directly (most compatible).
    Retries up to MAX_RETRIES times before declaring the site down.
    """
    for attempt in range(1 + MAX_RETRIES):
        try:
            start = time.time()
            req = Request(url, method="GET")
            req.add_header("User-Agent", "Render-Keep-Alive/2.0")

            resp = urlopen(req, timeout=REQUEST_TIMEOUT)
            latency = round((time.time() - start) * 1000, 2)

            if 200 <= resp.status < 300:
                print(f"  ✅ UP   | {resp.status} | {latency:>7} ms | {url}")
                return {"url": url, "status": "up", "code": resp.status, "latency_ms": latency}

        except HTTPError as e:
            print(f"  ⚠️ HTTP {e.code} | {url}")
        except Exception:
            pass

        if attempt < MAX_RETRIES:
            print(f"  🔄 RETRY {attempt + 1}/{MAX_RETRIES} | {url}")
            time.sleep(RETRY_DELAY)

    print(f"  ❌ DOWN | {url}")
    send_telegram_alert(url, "DOWN / Timeout")
    return {"url": url, "status": "down", "code": None, "latency_ms": None}


def ping_all(sites):
    """Ping ALL sites at the same time using a thread pool."""
    results = []
    with ThreadPoolExecutor(max_workers=min(len(sites), 20)) as pool:
        futures = {pool.submit(ping, url): url for url in sites}
        for future in as_completed(futures):
            results.append(future.result())
    return results


# ──────────────────── Background Pinger ─────────────────────── #

def background_pinger():
    global last_results, cycle_count

    sites = load_sites()
    print(f"\n🚀 Keep-Alive started — {len(sites)} site(s), every {PING_INTERVAL}s\n")

    # Initial ping immediately on startup
    while True:
        cycle_count += 1
        print(f"\n{'━' * 60}")
        print(f"  CYCLE #{cycle_count}  •  {ts()}")
        print(f"{'━' * 60}")

        results = ping_all(sites)
        last_results = results

        up   = sum(1 for r in results if r["status"] == "up")
        down = sum(1 for r in results if r["status"] == "down")
        print(f"\n  📊 Results: {up} up • {down} down")
        print(f"  ⏰ Next cycle in {PING_INTERVAL // 60} minutes\n")

        time.sleep(PING_INTERVAL)


# ────────────────── Web Server (Health Check) ───────────────── #

class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler — serves health check + latest results."""

    def _build_response(self):
        return json.dumps({
            "status": "alive",
            "timestamp": ts(),
            "cycle": cycle_count,
            "total_sites": len(last_results),
            "up":   sum(1 for r in last_results if r["status"] == "up"),
            "down": sum(1 for r in last_results if r["status"] == "down"),
            "results": last_results,
        }, indent=2).encode()

    def do_GET(self):
        body = self._build_response()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        body = self._build_response()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, format, *args):
        pass                                           # suppress noisy access logs


# ──────────────────────── Entry Point ───────────────────────── #

if __name__ == "__main__":
    # Start the background pinger thread
    pinger = threading.Thread(target=background_pinger, daemon=True)
    pinger.start()

    # Start the web server (Render needs this to keep the service alive)
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"🌐 Health server listening on port {PORT}")
    server.serve_forever()
