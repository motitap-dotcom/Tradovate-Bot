#!/usr/bin/env python3
"""
Bot Connection Verifier
========================
Comprehensive check: API connectivity, authentication, account status,
and server health. Writes results to verify_report.json.

Designed to run both:
  - In GitHub Actions (via system-status workflow)
  - On the VPS server (via cron or manual)

Usage:
    python verify_bot.py              # Run all checks
    python verify_bot.py --server     # Include server-local checks (systemd, logs)
"""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"
ACCOUNT_ID = 39996695
BOT_DIR = Path(__file__).parent
TOKEN_FILE = BOT_DIR / ".tradovate_token.json"
LOG_FILE = BOT_DIR / "bot.log"
LIVE_STATUS_FILE = BOT_DIR / "live_status.json"
REPORT_FILE = BOT_DIR / "verify_report.json"

# Tradovate web auth constants
_HMAC_KEY = "1259-11e7-485a-aeae-9b6016579351"


def _encrypt_password(name: str, password: str) -> str:
    offset = len(name) % len(password)
    rearranged = password[offset:] + password[:offset]
    reversed_pw = rearranged[::-1]
    return base64.b64encode(reversed_pw.encode()).decode()


def check_api_connectivity() -> dict:
    """Check if Tradovate API endpoints are reachable."""
    result = {"demo": False, "live": False, "demo_latency_ms": 0, "live_latency_ms": 0}

    for label, url in [("demo", DEMO_URL), ("live", LIVE_URL)]:
        try:
            import time
            t0 = time.time()
            r = requests.post(
                f"{url}/auth/accesstokenrequest",
                json={"name": "connectivity_test"},
                timeout=15,
            )
            latency = int((time.time() - t0) * 1000)
            result[label] = r.status_code in (200, 400, 401, 403)
            result[f"{label}_latency_ms"] = latency
            result[f"{label}_status"] = r.status_code
            print(f"  {label.upper()}: reachable (status={r.status_code}, {latency}ms)")
        except Exception as e:
            print(f"  {label.upper()}: UNREACHABLE - {e}")
            result[f"{label}_error"] = str(e)

    return result


def check_auth_token_file() -> dict:
    """Check if a saved token exists and is valid."""
    result = {"exists": False, "valid": False, "expired": False, "minutes_remaining": 0}

    if not TOKEN_FILE.exists():
        print("  Token file: NOT FOUND")
        return result

    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)

        result["exists"] = True
        token = data.get("accessToken", "")
        exp = data.get("expirationTime", "")

        if exp:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
            result["minutes_remaining"] = round(remaining, 1)
            result["expired"] = remaining <= 0
            print(f"  Token file: exists, expires in {remaining:.0f} min")

        if token and not result["expired"]:
            # Validate token against API
            try:
                r = requests.get(
                    f"{DEMO_URL}/account/list",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if r.status_code == 200:
                    result["valid"] = True
                    accounts = r.json()
                    result["accounts"] = len(accounts)
                    print(f"  Token: VALID ({len(accounts)} accounts)")
                else:
                    print(f"  Token: INVALID (status={r.status_code})")
            except Exception as e:
                print(f"  Token validation error: {e}")
        elif result["expired"]:
            print("  Token: EXPIRED")

    except Exception as e:
        print(f"  Token file error: {e}")
        result["error"] = str(e)

    return result


def check_web_auth() -> dict:
    """Attempt web-style authentication."""
    result = {"attempted": False, "success": False, "method": None}

    username = os.environ.get("TRADOVATE_USERNAME", "").strip()
    password = os.environ.get("TRADOVATE_PASSWORD", "").strip()

    if not username or not password:
        print("  No credentials available for web auth")
        return result

    result["attempted"] = True
    encrypted_pw = _encrypt_password(username, password)
    # Fixed device ID to avoid Tradovate rate-limiting new device registrations
    device_id = "verify-tradovate-bot-001"

    payload = {
        "name": username,
        "password": encrypted_pw,
        "appId": "tradovate_trader(web)",
        "appVersion": "3.260220.0",
        "deviceId": device_id,
        "cid": 8,
        "sec": "",
        "chl": "",
        "organization": "",
    }

    fields = ["chl", "deviceId", "name", "password", "appId"]
    message = "".join(str(payload.get(f, "")) for f in fields)
    payload["sec"] = hmac.new(
        _HMAC_KEY.encode(), message.encode(), hashlib.sha256
    ).hexdigest()

    for label, url in [("live", LIVE_URL), ("demo", DEMO_URL)]:
        try:
            r = requests.post(
                f"{url}/auth/accesstokenrequest", json=payload, timeout=30
            )
            data = r.json()

            if "accessToken" in data:
                result["success"] = True
                result["method"] = f"web_auth_{label}"
                result["user_id"] = data.get("userId")
                result["token_preview"] = data["accessToken"][:20] + "..."
                print(f"  {label.upper()} auth: SUCCESS (userId={data.get('userId')})")
                return result
            elif "p-ticket" in data:
                result["captcha_required"] = True
                result["method"] = "captcha_needed"
                print(f"  {label.upper()} auth: CAPTCHA required (credentials valid)")
                return result
            else:
                err = data.get("errorText", str(data))
                result["error"] = err
                print(f"  {label.upper()} auth: {err}")
        except Exception as e:
            print(f"  {label.upper()} auth error: {e}")
            result["error"] = str(e)

    return result


def check_account_status(token: str) -> dict:
    """Get account, balance, positions, and orders."""
    result = {"account": {}, "balance": {}, "positions": [], "orders": []}
    headers = {"Authorization": f"Bearer {token}"}

    # Account
    try:
        r = requests.get(f"{DEMO_URL}/account/list", headers=headers, timeout=10)
        if r.status_code == 200:
            for a in r.json():
                if a.get("id") == ACCOUNT_ID or len(r.json()) == 1:
                    result["account"] = {
                        "name": a.get("name"),
                        "id": a.get("id"),
                        "active": a.get("active"),
                    }
                    print(f"  Account: {a.get('name')} (id={a.get('id')})")
    except Exception as e:
        result["account_error"] = str(e)

    # Balance
    try:
        r = requests.get(
            f"{DEMO_URL}/cashBalance/getcashbalancesnapshot",
            headers=headers,
            timeout=10,
            params={"accountId": ACCOUNT_ID},
        )
        if r.status_code == 200:
            bal = r.json()
            result["balance"] = {
                "totalCashValue": bal.get("totalCashValue"),
                "netLiq": bal.get("netLiq"),
                "realizedPnl": bal.get("realizedPnl"),
                "unrealizedPnl": bal.get("unrealizedPnl"),
                "openPnL": bal.get("openPnL"),
            }
            print(f"  Balance: ${bal.get('totalCashValue', 'N/A')}")
            print(f"  P&L: realized=${bal.get('realizedPnl', 'N/A')} unrealized=${bal.get('openPnL', 'N/A')}")
    except Exception as e:
        result["balance_error"] = str(e)

    # Positions
    try:
        r = requests.get(f"{DEMO_URL}/position/list", headers=headers, timeout=10)
        if r.status_code == 200:
            open_pos = [p for p in r.json() if p.get("netPos", 0) != 0]
            result["positions"] = [
                {
                    "contractId": p.get("contractId"),
                    "netPos": p.get("netPos"),
                    "netPrice": p.get("netPrice"),
                }
                for p in open_pos
            ]
            print(f"  Open positions: {len(open_pos)}")
    except Exception as e:
        result["positions_error"] = str(e)

    # Orders
    try:
        r = requests.get(f"{DEMO_URL}/order/list", headers=headers, timeout=10)
        if r.status_code == 200:
            active = [
                o
                for o in r.json()
                if o.get("ordStatus") in ("Working", "Accepted")
            ]
            result["orders"] = [
                {
                    "id": o.get("id"),
                    "action": o.get("action"),
                    "qty": o.get("orderQty"),
                    "status": o.get("ordStatus"),
                }
                for o in active
            ]
            print(f"  Active orders: {len(active)}")
    except Exception as e:
        result["orders_error"] = str(e)

    return result


def check_server_local() -> dict:
    """Server-local checks: systemd service, logs, live_status.json."""
    result = {
        "bot_active": False,
        "bot_pid": None,
        "uptime": None,
        "last_log_lines": [],
        "live_status": {},
    }

    # systemctl status
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", "tradovate-bot"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        result["bot_active"] = out == "active"
        print(f"  Service: {out}")
    except subprocess.CalledProcessError:
        print("  Service: not running / not found")
    except FileNotFoundError:
        print("  systemctl: not available")
        return result

    if result["bot_active"]:
        try:
            pid = subprocess.check_output(
                ["systemctl", "show", "tradovate-bot", "--property=MainPID", "--value"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            result["bot_pid"] = pid
            print(f"  PID: {pid}")
        except Exception:
            pass

        try:
            uptime = subprocess.check_output(
                [
                    "systemctl",
                    "show",
                    "tradovate-bot",
                    "--property=ActiveEnterTimestamp",
                    "--value",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            result["uptime"] = uptime
            print(f"  Up since: {uptime}")
        except Exception:
            pass

    # Last log lines
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", "tradovate-bot", "--no-pager", "-n", "10"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        result["last_log_lines"] = out.strip().split("\n")[-10:]
        print(f"  Last logs: {len(result['last_log_lines'])} lines")
    except Exception:
        pass

    # bot.log
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text().strip().split("\n")[-10:]
            result["bot_log_tail"] = lines
            # Look for status line
            for line in reversed(lines):
                if "Status |" in line:
                    print(f"  Last status: {line.strip()}")
                    break
        except Exception:
            pass

    # live_status.json
    if LIVE_STATUS_FILE.exists():
        try:
            with open(LIVE_STATUS_FILE) as f:
                result["live_status"] = json.load(f)
            print(f"  live_status.json: found")
        except Exception:
            pass

    # System resources
    try:
        disk = subprocess.check_output(
            ["df", "-h", "/"], text=True, stderr=subprocess.DEVNULL
        )
        result["disk"] = disk.strip().split("\n")[-1].split()[4] if disk else "?"
    except Exception:
        pass

    try:
        mem = subprocess.check_output(
            ["free", "-m"], text=True, stderr=subprocess.DEVNULL
        )
        lines = mem.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            result["memory"] = f"{parts[2]}MB/{parts[1]}MB"
    except Exception:
        pass

    return result


def main():
    server_mode = "--server" in sys.argv

    print("=" * 60)
    print("  TRADOVATE BOT CONNECTION VERIFIER")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "server" if server_mode else "remote",
        "checks": {},
        "summary": {},
    }

    # 1. API Connectivity
    print("\n[1/5] API Connectivity")
    api = check_api_connectivity()
    report["checks"]["api"] = api

    # 2. Token File
    print("\n[2/5] Saved Token")
    token_info = check_auth_token_file()
    report["checks"]["token_file"] = token_info

    # 3. Web Auth
    print("\n[3/5] Web Authentication")
    auth = check_web_auth()
    report["checks"]["web_auth"] = auth

    # 4. Account Status (if we have a valid token)
    print("\n[4/5] Account Status")
    active_token = None

    # Use token from file if valid
    if token_info.get("valid") and TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            active_token = json.load(f).get("accessToken")

    # Or from env
    if not active_token:
        env_token = os.environ.get("TRADOVATE_ACCESS_TOKEN", "").strip()
        if env_token:
            active_token = env_token

    if active_token:
        account = check_account_status(active_token)
        report["checks"]["account"] = account
    else:
        print("  No valid token available — skipping account check")
        report["checks"]["account"] = {"skipped": True, "reason": "no_valid_token"}

    # 5. Server-local checks (only on the VPS)
    print("\n[5/5] Server Status")
    if server_mode:
        server = check_server_local()
        report["checks"]["server"] = server
    else:
        print("  Skipped (not in --server mode)")
        report["checks"]["server"] = {"skipped": True}

    # Summary
    api_ok = api.get("demo") or api.get("live")
    auth_ok = token_info.get("valid") or auth.get("success")
    has_balance = bool(report["checks"].get("account", {}).get("balance", {}).get("totalCashValue"))
    bot_running = report["checks"].get("server", {}).get("bot_active", False)

    report["summary"] = {
        "api_reachable": api_ok,
        "authenticated": auth_ok,
        "has_account_data": has_balance,
        "bot_running": bot_running if server_mode else "unknown",
        "overall": "OK" if (api_ok and auth_ok and has_balance) else "ISSUES_FOUND",
    }

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  API Reachable:    {'YES' if api_ok else 'NO'}")
    print(f"  Authenticated:    {'YES' if auth_ok else 'NO'}")
    print(f"  Account Data:     {'YES' if has_balance else 'NO'}")
    if server_mode:
        print(f"  Bot Running:      {'YES' if bot_running else 'NO'}")
    print(f"  Overall:          {report['summary']['overall']}")
    print("=" * 60)

    # Write report
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {REPORT_FILE}")

    return 0 if report["summary"]["overall"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
