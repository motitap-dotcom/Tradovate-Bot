#!/usr/bin/env python3
"""
Bot Health Check — Server-Side Deep Verification
==================================================
Runs ON THE SERVER to verify the bot is alive, authenticated,
receiving market data, and the account is healthy.

Designed to be called by server_cron.sh every 5 minutes.
Writes detailed results to bot_health.json.

Usage:
    python bot_health_check.py          # Full health check
    python bot_health_check.py --quick  # Quick check (no WS test)
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed")
    sys.exit(1)

BOT_DIR = Path(__file__).parent
TOKEN_FILE = BOT_DIR / ".tradovate_token.json"
LOG_FILE = BOT_DIR / "bot.log"
LIVE_STATUS_FILE = BOT_DIR / "live_status.json"
HEALTH_FILE = BOT_DIR / "bot_health.json"

DEMO_URL = "https://demo.tradovateapi.com/v1"
ACCOUNT_ID = 39996695


def check_bot_process() -> dict:
    """Check if bot.py is running as a systemd service or process."""
    result = {
        "running": False,
        "pid": None,
        "uptime_since": None,
        "uptime_minutes": None,
        "service_status": "unknown",
    }

    # Check systemd service first
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", "tradovate-bot"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        result["service_status"] = out
        result["running"] = out == "active"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if result["running"]:
        try:
            pid = subprocess.check_output(
                ["systemctl", "show", "tradovate-bot", "--property=MainPID", "--value"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            result["pid"] = int(pid) if pid and pid != "0" else None
        except Exception:
            pass

        try:
            uptime_str = subprocess.check_output(
                ["systemctl", "show", "tradovate-bot",
                 "--property=ActiveEnterTimestamp", "--value"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if uptime_str:
                result["uptime_since"] = uptime_str
                # Parse and compute minutes
                try:
                    from dateutil.parser import parse
                    up_dt = parse(uptime_str)
                    result["uptime_minutes"] = round(
                        (datetime.now(timezone.utc) - up_dt.astimezone(timezone.utc)).total_seconds() / 60
                    )
                except Exception:
                    pass
        except Exception:
            pass
    else:
        # Fallback: check process directly
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", "python.*bot.py"],
                text=True, stderr=subprocess.DEVNULL,
            )
            pids = [int(p) for p in out.strip().split("\n") if p]
            if pids:
                result["running"] = True
                result["pid"] = pids[0]
                result["service_status"] = "running (process)"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    return result


def check_token() -> dict:
    """Check if the saved token exists and is valid."""
    result = {
        "exists": False,
        "valid": False,
        "expired": False,
        "minutes_remaining": 0,
        "account_id": None,
    }

    if not TOKEN_FILE.exists():
        return result

    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)

        result["exists"] = True
        token = data.get("accessToken", "")
        exp = data.get("expirationTime", "")
        result["account_id"] = data.get("accountId")

        if exp:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
            result["minutes_remaining"] = round(remaining, 1)
            result["expired"] = remaining <= 0

        if token and not result["expired"]:
            # Quick validation — hit a lightweight endpoint
            try:
                r = requests.get(
                    f"{DEMO_URL}/account/list",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                result["valid"] = r.status_code == 200
            except Exception:
                pass

    except Exception as e:
        result["error"] = str(e)

    return result


def check_account(token: str) -> dict:
    """Get account balance, positions, orders."""
    result = {
        "balance": None,
        "net_liq": None,
        "realized_pnl": None,
        "unrealized_pnl": None,
        "open_positions": 0,
        "active_orders": 0,
        "positions_detail": [],
        "orders_detail": [],
    }
    headers = {"Authorization": f"Bearer {token}"}

    # Balance
    try:
        r = requests.post(
            f"{DEMO_URL}/cashBalance/getcashbalancesnapshot",
            json={"accountId": ACCOUNT_ID},
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            bal = r.json()
            result["balance"] = bal.get("totalCashValue")
            result["net_liq"] = bal.get("netLiq")
            result["realized_pnl"] = bal.get("realizedPnl")
            result["unrealized_pnl"] = bal.get("openPnL")
    except Exception:
        pass

    # Positions
    try:
        r = requests.get(f"{DEMO_URL}/position/list", headers=headers, timeout=10)
        if r.status_code == 200:
            open_pos = [p for p in r.json() if p.get("netPos", 0) != 0]
            result["open_positions"] = len(open_pos)
            result["positions_detail"] = [
                {
                    "contractId": p.get("contractId"),
                    "netPos": p.get("netPos"),
                    "netPrice": p.get("netPrice"),
                }
                for p in open_pos
            ]
    except Exception:
        pass

    # Orders
    try:
        r = requests.get(f"{DEMO_URL}/order/list", headers=headers, timeout=10)
        if r.status_code == 200:
            active = [o for o in r.json()
                       if o.get("ordStatus") in ("Working", "Accepted")]
            result["active_orders"] = len(active)
            result["orders_detail"] = [
                {
                    "id": o.get("id"),
                    "action": o.get("action"),
                    "qty": o.get("orderQty"),
                    "status": o.get("ordStatus"),
                    "contractId": o.get("contractId"),
                }
                for o in active
            ]
    except Exception:
        pass

    return result


def check_market_data_ws(token: str) -> dict:
    """Quick WebSocket connection test — connect, auth, disconnect."""
    result = {"ws_connected": False, "ws_auth_ok": False, "ws_latency_ms": None}

    try:
        import websocket
        ws_url = "wss://md-demo.tradovateapi.com/v1/websocket"

        t0 = time.time()
        ws = websocket.create_connection(ws_url, timeout=10)

        # Tradovate WS protocol: first message is a welcome frame
        welcome = ws.recv()
        if welcome:
            result["ws_connected"] = True

        # Authenticate
        auth_msg = f"authorize\n1\n{json.dumps({'token': token})}"
        ws.send(auth_msg)
        auth_resp = ws.recv()

        latency = int((time.time() - t0) * 1000)
        result["ws_latency_ms"] = latency

        if auth_resp and '"s":200' in auth_resp:
            result["ws_auth_ok"] = True

        ws.close()
    except ImportError:
        result["error"] = "websocket-client not installed"
    except Exception as e:
        result["error"] = str(e)

    return result


def check_bot_log() -> dict:
    """Analyze bot.log for recent activity and health indicators."""
    result = {
        "log_exists": False,
        "last_status_line": None,
        "last_status_age_seconds": None,
        "last_lines": [],
        "errors_last_hour": 0,
        "warnings_last_hour": 0,
        "signals_today": 0,
        "trades_today": 0,
    }

    if not LOG_FILE.exists():
        return result

    result["log_exists"] = True

    try:
        # Read last 200 lines
        with open(LOG_FILE) as f:
            lines = f.readlines()

        tail = lines[-200:] if len(lines) > 200 else lines
        result["last_lines"] = [l.strip() for l in tail[-15:]]

        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        for line in reversed(tail):
            line = line.strip()

            # Last status line
            if "Status |" in line and result["last_status_line"] is None:
                result["last_status_line"] = line
                # Parse timestamp
                try:
                    ts_str = line[:19]  # "2026-03-01 14:30:00"
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    ts = ts.replace(tzinfo=timezone.utc)
                    age = (now - ts).total_seconds()
                    result["last_status_age_seconds"] = int(age)
                except Exception:
                    pass

            # Count errors/warnings in last hour
            try:
                ts_str = line[:19]
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if ts >= one_hour_ago:
                    if "[ERROR]" in line:
                        result["errors_last_hour"] += 1
                    if "[WARNING]" in line:
                        result["warnings_last_hour"] += 1
                    if "SIGNAL:" in line:
                        result["signals_today"] += 1
                    if "Order placed:" in line:
                        result["trades_today"] += 1
            except Exception:
                pass

    except Exception as e:
        result["error"] = str(e)

    return result


def check_live_status() -> dict:
    """Read live_status.json written by the bot itself."""
    if not LIVE_STATUS_FILE.exists():
        return {"exists": False}

    try:
        with open(LIVE_STATUS_FILE) as f:
            data = json.load(f)

        # Calculate age
        ts = data.get("timestamp", "")
        age = None
        if ts:
            try:
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = int((datetime.now(timezone.utc) - ts_dt).total_seconds())
            except Exception:
                pass

        return {
            "exists": True,
            "age_seconds": age,
            "data": data,
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def check_system_resources() -> dict:
    """Get disk, memory, CPU usage."""
    result = {}

    try:
        disk = subprocess.check_output(
            ["df", "-h", "/"], text=True, stderr=subprocess.DEVNULL
        )
        parts = disk.strip().split("\n")[-1].split()
        result["disk_usage"] = parts[4] if len(parts) > 4 else "?"
        result["disk_available"] = parts[3] if len(parts) > 3 else "?"
    except Exception:
        pass

    try:
        mem = subprocess.check_output(
            ["free", "-m"], text=True, stderr=subprocess.DEVNULL
        )
        mem_parts = mem.strip().split("\n")[1].split()
        result["memory_used_mb"] = int(mem_parts[2])
        result["memory_total_mb"] = int(mem_parts[1])
        result["memory_pct"] = round(int(mem_parts[2]) / int(mem_parts[1]) * 100, 1)
    except Exception:
        pass

    try:
        load = subprocess.check_output(
            ["cat", "/proc/loadavg"], text=True, stderr=subprocess.DEVNULL
        )
        result["load_avg"] = load.strip().split()[:3]
    except Exception:
        pass

    return result


def main():
    quick = "--quick" in sys.argv

    print("=" * 60)
    print("  BOT HEALTH CHECK")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    health = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "verdict": {},
    }

    # 1. Bot process
    print("\n[1/6] Bot Process")
    proc = check_bot_process()
    health["checks"]["process"] = proc
    print(f"  Running: {proc['running']} | PID: {proc.get('pid')} | Uptime: {proc.get('uptime_minutes', '?')} min")

    # 2. Token
    print("\n[2/6] Auth Token")
    tok = check_token()
    health["checks"]["token"] = tok
    print(f"  Exists: {tok['exists']} | Valid: {tok['valid']} | Remaining: {tok['minutes_remaining']} min")

    # 3. Account (if we have a valid token)
    token_str = None
    if tok["valid"] and TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            token_str = json.load(f).get("accessToken")

    print("\n[3/6] Account Status")
    if token_str:
        acct = check_account(token_str)
        health["checks"]["account"] = acct
        print(f"  Balance: ${acct.get('balance', 'N/A')}")
        print(f"  Net Liq: ${acct.get('net_liq', 'N/A')}")
        print(f"  Realized P&L: ${acct.get('realized_pnl', 'N/A')}")
        print(f"  Unrealized P&L: ${acct.get('unrealized_pnl', 'N/A')}")
        print(f"  Open positions: {acct['open_positions']}")
        print(f"  Active orders: {acct['active_orders']}")
    else:
        health["checks"]["account"] = {"skipped": True, "reason": "no_valid_token"}
        print("  Skipped (no valid token)")

    # 4. WebSocket test
    print("\n[4/6] Market Data WebSocket")
    if token_str and not quick:
        ws = check_market_data_ws(token_str)
        health["checks"]["websocket"] = ws
        print(f"  Connected: {ws['ws_connected']} | Auth: {ws['ws_auth_ok']} | Latency: {ws.get('ws_latency_ms', '?')}ms")
    else:
        health["checks"]["websocket"] = {"skipped": True}
        print(f"  Skipped ({'quick mode' if quick else 'no token'})")

    # 5. Bot log analysis
    print("\n[5/6] Bot Log")
    log = check_bot_log()
    health["checks"]["log"] = log
    print(f"  Log exists: {log['log_exists']}")
    if log.get("last_status_line"):
        print(f"  Last status: {log['last_status_age_seconds']}s ago")
    print(f"  Errors (1h): {log['errors_last_hour']} | Warnings: {log['warnings_last_hour']}")
    print(f"  Signals today: {log['signals_today']} | Trades: {log['trades_today']}")

    # 6. Live status + System resources
    print("\n[6/6] System")
    live = check_live_status()
    health["checks"]["live_status"] = live
    if live.get("exists"):
        print(f"  live_status.json: exists (age: {live.get('age_seconds', '?')}s)")
    else:
        print("  live_status.json: not found")

    resources = check_system_resources()
    health["checks"]["system"] = resources
    if resources:
        print(f"  Disk: {resources.get('disk_usage', '?')} | Memory: {resources.get('memory_pct', '?')}%")

    # Verdict
    bot_alive = proc["running"]
    token_ok = tok["valid"]
    has_balance = bool(health["checks"].get("account", {}).get("balance"))
    ws_ok = health["checks"].get("websocket", {}).get("ws_auth_ok", False)
    log_fresh = (log.get("last_status_age_seconds") or 9999) < 120  # <2 min old

    overall = "HEALTHY" if (bot_alive and token_ok and has_balance and log_fresh) else \
              "DEGRADED" if (bot_alive and (token_ok or log_fresh)) else \
              "DOWN"

    health["verdict"] = {
        "bot_alive": bot_alive,
        "token_valid": token_ok,
        "has_balance_data": has_balance,
        "ws_connected": ws_ok,
        "log_fresh": log_fresh,
        "overall": overall,
    }

    print("\n" + "=" * 60)
    print("  VERDICT")
    print("=" * 60)
    print(f"  Bot Alive:      {'YES' if bot_alive else 'NO'}")
    print(f"  Token Valid:    {'YES' if token_ok else 'NO'}")
    print(f"  Balance Data:   {'YES' if has_balance else 'NO'}")
    print(f"  WS Connected:   {'YES' if ws_ok else 'SKIP' if quick else 'NO'}")
    print(f"  Log Fresh:      {'YES' if log_fresh else 'NO'}")
    print(f"  Overall:        {overall}")
    print("=" * 60)

    # Write health report
    with open(HEALTH_FILE, "w") as f:
        json.dump(health, f, indent=2)
    print(f"\nReport: {HEALTH_FILE}")

    return 0 if overall in ("HEALTHY", "DEGRADED") else 1


if __name__ == "__main__":
    sys.exit(main())
