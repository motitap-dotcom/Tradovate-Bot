#!/usr/bin/env python3
"""
Remote Management API for Tradovate Bot
=========================================
Lightweight HTTP API that runs on the VPS alongside the bot.
Allows remote control (start/stop/restart/status/logs) from anywhere.

Usage:
    python remote_api.py                # Start on port 9090
    MGMT_PORT=8080 python remote_api.py # Custom port

Security:
    Set MGMT_API_KEY in .env — all requests must include header:
        X-API-Key: <your-key>

Endpoints:
    GET  /ping              — Health check
    GET  /status            — Full bot status (balance, P&L, positions, etc.)
    GET  /logs              — Recent log lines (?lines=50)
    GET  /logs/activity     — Recent signals, trades, locks
    GET  /journal           — Trade journal summary
    GET  /journal/trades    — All trades list
    GET  /token             — Auth token status
    POST /bot/start         — Start the bot via systemd
    POST /bot/stop          — Stop the bot via systemd
    POST /bot/restart       — Restart the bot via systemd
    POST /bot/update        — Git pull + restart
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BOT_DIR, "bot.log")
TOKEN_FILE = os.path.join(BOT_DIR, ".tradovate_token.json")
JOURNAL_FILE = os.path.join(BOT_DIR, "trade_journal.json")
ENV_FILE = os.path.join(BOT_DIR, ".env")
SERVICE_NAME = "tradovate-bot"

# Load .env manually (minimal, no dependency)
_env_cache = {}


def _load_env():
    global _env_cache
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    _env_cache[k.strip()] = v.strip()


_load_env()
API_KEY = os.environ.get("MGMT_API_KEY") or _env_cache.get("MGMT_API_KEY", "")
PORT = int(os.environ.get("MGMT_PORT") or _env_cache.get("MGMT_PORT", "9090"))


# ── Helper functions (adapted from status.py) ──────────────


def is_bot_running():
    """Check if bot.py process is running."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "python.*bot\\.py"], text=True, stderr=subprocess.DEVNULL
        )
        lines = [l for l in out.strip().split("\n") if l and "remote_api" not in l]
        if lines:
            pid = int(lines[0].split()[0])
            return True, pid
        return False, 0
    except subprocess.CalledProcessError:
        return False, 0


def get_systemd_status():
    """Get systemd service status."""
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", SERVICE_NAME], text=True, stderr=subprocess.DEVNULL
        ).strip()
        return out  # "active", "inactive", "failed"
    except subprocess.CalledProcessError as e:
        return e.output.strip() if e.output else "unknown"


def get_last_status():
    """Parse most recent status line from bot.log."""
    result = {
        "balance": 0, "day_pnl": 0, "to_floor": 0,
        "contracts": "0/0", "trades": "0/0", "locked": False,
        "timestamp": "", "age_seconds": -1,
    }
    if not os.path.exists(LOG_FILE):
        return result
    try:
        out = subprocess.check_output(
            ["tail", "-100", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return result

    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Status \| "
        r"balance=([\d.]+) \| day_pnl=([-\d.]+) \| to_floor=([-\d.]+) \| "
        r"contracts=(\d+/\d+) \| trades=(\d+/\d+) \| locked=(\w+)"
    )
    for line in reversed(out.strip().split("\n")):
        m = pattern.search(line)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            return {
                "balance": float(m.group(2)),
                "day_pnl": float(m.group(3)),
                "to_floor": float(m.group(4)),
                "contracts": m.group(5),
                "trades": m.group(6),
                "locked": m.group(7) == "True",
                "timestamp": m.group(1),
                "age_seconds": round(age, 1),
            }
    return result


def get_recent_activity(max_lines=15):
    """Get recent trade signals, entries, exits, and lock events."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", "-500", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return []
    keywords = ["SIGNAL:", "LOCKED", "bracket order", "Force close",
                "Journal: ENTRY", "Journal: EXIT", "Authenticated",
                "Bot starting", "Bot stopped", "DRAWDOWN", "DAILY LOSS"]
    items = []
    for line in out.strip().split("\n"):
        if any(kw in line for kw in keywords):
            items.append(line.strip())
    return items[-max_lines:]


def get_log_lines(n=50):
    """Get last N lines from bot.log."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", f"-{n}", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip().split("\n")
    except subprocess.CalledProcessError:
        return []


def get_token_status():
    """Check auth token validity."""
    if not os.path.exists(TOKEN_FILE):
        return {"status": "missing", "remaining_minutes": 0}
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        exp = data.get("expirationTime", "")
        if not exp:
            return {"status": "unknown", "remaining_minutes": 0}
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
        if remaining <= 0:
            return {"status": "expired", "remaining_minutes": round(remaining, 1)}
        return {"status": "valid", "remaining_minutes": round(remaining, 1)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def get_journal_summary():
    """Get trade journal summary."""
    if not os.path.exists(JOURNAL_FILE):
        return {"trades": 0}
    try:
        with open(JOURNAL_FILE) as f:
            data = json.load(f)
        return data.get("summary", {"trades": 0})
    except Exception:
        return {"trades": 0}


def get_journal_trades():
    """Get all trades from journal."""
    if not os.path.exists(JOURNAL_FILE):
        return []
    try:
        with open(JOURNAL_FILE) as f:
            data = json.load(f)
        return data.get("trades", [])
    except Exception:
        return []


def run_systemctl(action):
    """Run systemctl start/stop/restart on the bot service."""
    if action not in ("start", "stop", "restart"):
        return False, "Invalid action"
    try:
        subprocess.check_output(
            ["systemctl", action, SERVICE_NAME],
            text=True, stderr=subprocess.STDOUT, timeout=30
        )
        time.sleep(1)
        new_status = get_systemd_status()
        return True, f"Service {action}ed. Status: {new_status}"
    except subprocess.CalledProcessError as e:
        return False, f"systemctl {action} failed: {e.output}"
    except subprocess.TimeoutExpired:
        return False, f"systemctl {action} timed out"


def git_pull_and_restart():
    """Pull latest code and restart the bot."""
    results = []
    try:
        # Stop bot first
        subprocess.check_output(
            ["systemctl", "stop", SERVICE_NAME],
            text=True, stderr=subprocess.STDOUT, timeout=15
        )
        results.append("Bot stopped.")
    except Exception as e:
        results.append(f"Stop warning: {e}")

    try:
        out = subprocess.check_output(
            ["git", "-C", BOT_DIR, "pull"],
            text=True, stderr=subprocess.STDOUT, timeout=30
        )
        results.append(f"Git pull: {out.strip()}")
    except Exception as e:
        results.append(f"Git pull failed: {e}")
        return False, "\n".join(results)

    try:
        subprocess.check_output(
            ["systemctl", "start", SERVICE_NAME],
            text=True, stderr=subprocess.STDOUT, timeout=15
        )
        time.sleep(1)
        results.append(f"Bot restarted. Status: {get_systemd_status()}")
    except Exception as e:
        results.append(f"Start failed: {e}")
        return False, "\n".join(results)

    return True, "\n".join(results)


# ── HTTP API Handler ──────────────────────────────────────


class ManagementHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the management API."""

    def log_message(self, format, *args):
        """Suppress default logging to stderr."""
        pass

    def _check_auth(self):
        """Validate API key. Returns True if OK."""
        if not API_KEY:
            # No key configured — reject all requests for safety
            self._respond(403, {"error": "MGMT_API_KEY not configured on server"})
            return False
        provided = self.headers.get("X-API-Key", "")
        if not secrets.compare_digest(provided, API_KEY):
            self._respond(401, {"error": "Invalid API key"})
            return False
        return True

    def _respond(self, code, data):
        """Send JSON response."""
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_qs(self):
        """Parse query string parameters."""
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        if not self._check_auth():
            return

        path, qs = self._parse_qs()

        if path == "/ping":
            self._respond(200, {"ok": True, "time": datetime.now(timezone.utc).isoformat()})

        elif path == "/status":
            running, pid = is_bot_running()
            systemd = get_systemd_status()
            status = get_last_status()
            token = get_token_status()
            journal = get_journal_summary()
            self._respond(200, {
                "bot": {
                    "running": running,
                    "pid": pid,
                    "systemd": systemd,
                },
                "trading": status,
                "token": token,
                "journal": journal,
                "server_time": datetime.now(timezone.utc).isoformat(),
            })

        elif path == "/logs":
            n = int(qs.get("lines", ["50"])[0])
            n = min(n, 500)  # cap at 500
            lines = get_log_lines(n)
            self._respond(200, {"lines": lines, "count": len(lines)})

        elif path == "/logs/activity":
            n = int(qs.get("lines", ["15"])[0])
            activity = get_recent_activity(max_lines=n)
            self._respond(200, {"activity": activity, "count": len(activity)})

        elif path == "/journal":
            summary = get_journal_summary()
            self._respond(200, {"summary": summary})

        elif path == "/journal/trades":
            trades = get_journal_trades()
            self._respond(200, {"trades": trades, "count": len(trades)})

        elif path == "/token":
            token = get_token_status()
            self._respond(200, token)

        else:
            self._respond(404, {"error": f"Not found: {path}"})

    def do_POST(self):
        if not self._check_auth():
            return

        path, qs = self._parse_qs()

        if path == "/bot/start":
            ok, msg = run_systemctl("start")
            self._respond(200 if ok else 500, {"ok": ok, "message": msg})

        elif path == "/bot/stop":
            ok, msg = run_systemctl("stop")
            self._respond(200 if ok else 500, {"ok": ok, "message": msg})

        elif path == "/bot/restart":
            ok, msg = run_systemctl("restart")
            self._respond(200 if ok else 500, {"ok": ok, "message": msg})

        elif path == "/bot/update":
            ok, msg = git_pull_and_restart()
            self._respond(200 if ok else 500, {"ok": ok, "message": msg})

        else:
            self._respond(404, {"error": f"Not found: {path}"})


# ── Entry point ──────────────────────────────────────────


def main():
    if not API_KEY:
        print("=" * 55)
        print("  WARNING: MGMT_API_KEY is not set!")
        print("  Generate one and add to .env:")
        key = secrets.token_urlsafe(32)
        print(f"    MGMT_API_KEY={key}")
        print("=" * 55)
        print()

    server = HTTPServer(("0.0.0.0", PORT), ManagementHandler)
    print(f"Management API running on http://0.0.0.0:{PORT}")
    print(f"API key configured: {'YES' if API_KEY else 'NO (all requests will be rejected)'}")
    print(f"Bot service: {SERVICE_NAME}")
    print()
    print("Endpoints:")
    print(f"  GET  http://<vps-ip>:{PORT}/ping")
    print(f"  GET  http://<vps-ip>:{PORT}/status")
    print(f"  GET  http://<vps-ip>:{PORT}/logs?lines=50")
    print(f"  GET  http://<vps-ip>:{PORT}/logs/activity")
    print(f"  GET  http://<vps-ip>:{PORT}/journal")
    print(f"  GET  http://<vps-ip>:{PORT}/token")
    print(f"  POST http://<vps-ip>:{PORT}/bot/start")
    print(f"  POST http://<vps-ip>:{PORT}/bot/stop")
    print(f"  POST http://<vps-ip>:{PORT}/bot/restart")
    print(f"  POST http://<vps-ip>:{PORT}/bot/update")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down management API.")
        server.shutdown()


if __name__ == "__main__":
    main()
