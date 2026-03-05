#!/usr/bin/env python3
"""
Connection Check — Push & Listen Verification
===============================================
Creates a ping request, pushes to main via auto-merge,
and reads back the server's response.

This script is designed to be run by the server_cron.sh after
code is pulled. It performs a comprehensive health check and
writes the results to connection_status.json.

Usage:
    python connection_check.py                # Run server-side health check
    python connection_check.py --create-ping  # Create a ping request (run locally)
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

BOT_DIR = Path(__file__).parent
PING_FILE = BOT_DIR / "ping_request.json"
STATUS_FILE = BOT_DIR / "connection_status.json"
TOKEN_FILE = BOT_DIR / ".tradovate_token.json"
LOG_FILE = BOT_DIR / "bot.log"
LIVE_STATUS_FILE = BOT_DIR / "live_status.json"


def create_ping():
    """Create a ping request file that the server will respond to."""
    ping = {
        "ping_id": str(uuid.uuid4())[:8],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "claude-session",
        "message": "verify-bot-alive",
    }
    PING_FILE.write_text(json.dumps(ping, indent=2))
    print(f"Ping created: {ping['ping_id']}")
    return ping


def check_bot_process() -> dict:
    """Check if bot.py is running."""
    result = {"running": False, "pid": None, "service": "unknown", "uptime_minutes": None}

    # Check systemd service
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", "tradovate-bot"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        result["service"] = out
        result["running"] = out == "active"
    except (subprocess.CalledProcessError, FileNotFoundError):
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
                result["service"] = "process"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return result

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
            ts = subprocess.check_output(
                ["systemctl", "show", "tradovate-bot",
                 "--property=ActiveEnterTimestamp", "--value"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if ts:
                result["uptime_since"] = ts
                try:
                    from dateutil.parser import parse
                    up_dt = parse(ts)
                    result["uptime_minutes"] = round(
                        (datetime.now(timezone.utc) - up_dt.astimezone(timezone.utc)).total_seconds() / 60
                    )
                except Exception:
                    pass
        except Exception:
            pass

    return result


def check_token() -> dict:
    """Check saved token validity."""
    result = {"exists": False, "valid": False, "minutes_remaining": 0}

    if not TOKEN_FILE.exists():
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

        if token and not result.get("expired", True):
            try:
                import requests
                r = requests.get(
                    "https://demo.tradovateapi.com/v1/account/list",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                result["valid"] = r.status_code == 200
                if r.status_code == 200:
                    accounts = r.json()
                    result["accounts"] = len(accounts)
            except Exception as e:
                result["validation_error"] = str(e)

    except Exception as e:
        result["error"] = str(e)

    return result


def check_account(token: str) -> dict:
    """Get account balance and positions."""
    import requests
    result = {}
    headers = {"Authorization": f"Bearer {token}"}
    account_id = 39996695

    try:
        r = requests.post(
            "https://demo.tradovateapi.com/v1/cashBalance/getcashbalancesnapshot",
            json={"accountId": account_id},
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            bal = r.json()
            result["balance"] = bal.get("totalCashValue")
            result["net_liq"] = bal.get("netLiq")
            result["realized_pnl"] = bal.get("realizedPnl")
            result["unrealized_pnl"] = bal.get("openPnL")
    except Exception as e:
        result["balance_error"] = str(e)

    try:
        r = requests.get(
            "https://demo.tradovateapi.com/v1/position/list",
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            open_pos = [p for p in r.json() if p.get("netPos", 0) != 0]
            result["open_positions"] = len(open_pos)
            result["positions"] = [
                {"contractId": p.get("contractId"), "netPos": p.get("netPos"),
                 "netPrice": p.get("netPrice")} for p in open_pos
            ]
    except Exception as e:
        result["positions_error"] = str(e)

    try:
        r = requests.get(
            "https://demo.tradovateapi.com/v1/order/list",
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            active = [o for o in r.json()
                       if o.get("ordStatus") in ("Working", "Accepted")]
            result["active_orders"] = len(active)
    except Exception as e:
        result["orders_error"] = str(e)

    return result


def check_bot_log() -> dict:
    """Analyze bot.log for activity."""
    result = {"exists": False, "last_status": None, "last_status_age_sec": None,
              "errors_1h": 0, "warnings_1h": 0, "signals_today": 0,
              "last_lines": []}

    if not LOG_FILE.exists():
        return result
    result["exists"] = True

    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        tail = lines[-200:] if len(lines) > 200 else lines
        result["last_lines"] = [l.strip() for l in tail[-10:]]

        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        for line in reversed(tail):
            line = line.strip()
            if "Status |" in line and result["last_status"] is None:
                result["last_status"] = line
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    result["last_status_age_sec"] = int((now - ts).total_seconds())
                except Exception:
                    pass

            try:
                ts_str = line[:19]
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if ts >= one_hour_ago:
                    if "[ERROR]" in line:
                        result["errors_1h"] += 1
                    if "[WARNING]" in line:
                        result["warnings_1h"] += 1
                    if "SIGNAL:" in line:
                        result["signals_today"] += 1
            except Exception:
                pass
    except Exception as e:
        result["error"] = str(e)

    return result


def check_live_status() -> dict:
    """Read live_status.json written by the running bot."""
    if not LIVE_STATUS_FILE.exists():
        return {"exists": False}
    try:
        with open(LIVE_STATUS_FILE) as f:
            data = json.load(f)
        age = None
        ts = data.get("timestamp", "")
        if ts:
            try:
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = int((datetime.now(timezone.utc) - ts_dt).total_seconds())
            except Exception:
                pass
        return {"exists": True, "age_seconds": age, "data": data}
    except Exception as e:
        return {"exists": False, "error": str(e)}


def check_system() -> dict:
    """System resources."""
    result = {}
    try:
        disk = subprocess.check_output(["df", "-h", "/"], text=True, stderr=subprocess.DEVNULL)
        parts = disk.strip().split("\n")[-1].split()
        result["disk_usage"] = parts[4] if len(parts) > 4 else "?"
    except Exception:
        pass
    try:
        mem = subprocess.check_output(["free", "-m"], text=True, stderr=subprocess.DEVNULL)
        parts = mem.strip().split("\n")[1].split()
        result["memory_used_mb"] = int(parts[2])
        result["memory_total_mb"] = int(parts[1])
        result["memory_pct"] = round(int(parts[2]) / int(parts[1]) * 100, 1)
    except Exception:
        pass
    try:
        load = subprocess.check_output(["cat", "/proc/loadavg"], text=True, stderr=subprocess.DEVNULL)
        result["load_avg"] = load.strip().split()[:3]
    except Exception:
        pass
    return result


def run_health_check():
    """Full server-side health check."""
    print("=" * 60)
    print("  CONNECTION CHECK — Server Health Report")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_commit": "unknown",
    }

    # Read ping request if exists
    if PING_FILE.exists():
        try:
            ping = json.loads(PING_FILE.read_text())
            report["ping_response"] = {
                "ping_id": ping.get("ping_id"),
                "received_at": datetime.now(timezone.utc).isoformat(),
                "latency_note": "ping received and processed",
            }
            print(f"\n  Ping ID: {ping.get('ping_id')} — RECEIVED")
        except Exception:
            pass

    # Git info
    try:
        commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %s"], text=True,
            stderr=subprocess.DEVNULL, cwd=str(BOT_DIR),
        ).strip()
        report["git_commit"] = commit
    except Exception:
        pass

    # 1. Bot process
    print("\n[1/5] Bot Process")
    proc = check_bot_process()
    report["bot_process"] = proc
    status_str = "RUNNING" if proc["running"] else "STOPPED"
    print(f"  Status: {status_str} (service={proc['service']}, pid={proc.get('pid')})")
    if proc.get("uptime_minutes"):
        print(f"  Uptime: {proc['uptime_minutes']} minutes")

    # 2. Auth token
    print("\n[2/5] Auth Token")
    tok = check_token()
    report["token"] = tok
    if tok["valid"]:
        print(f"  Token: VALID ({tok['minutes_remaining']:.0f} min remaining)")
    elif tok["exists"]:
        print(f"  Token: EXISTS but {'EXPIRED' if tok.get('expired') else 'INVALID'}")
    else:
        print("  Token: NOT FOUND")

    # 3. Account (if token valid)
    print("\n[3/5] Account & Balance")
    if tok["valid"] and TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            token_str = json.load(f).get("accessToken")
        if token_str:
            acct = check_account(token_str)
            report["account"] = acct
            if acct.get("balance"):
                print(f"  Balance: ${acct['balance']:,.2f}")
                print(f"  Net Liq: ${acct.get('net_liq', 0):,.2f}")
                print(f"  Open positions: {acct.get('open_positions', 0)}")
                print(f"  Active orders: {acct.get('active_orders', 0)}")
            else:
                print("  Balance: unavailable")
        else:
            report["account"] = {"skipped": True, "reason": "no_token_string"}
            print("  Skipped (token string missing)")
    else:
        report["account"] = {"skipped": True, "reason": "no_valid_token"}
        print("  Skipped (no valid token)")

    # 4. Bot log
    print("\n[4/5] Bot Activity")
    log = check_bot_log()
    report["bot_log"] = {k: v for k, v in log.items() if k != "last_lines"}
    report["bot_log"]["last_lines"] = log.get("last_lines", [])[-5:]
    if log.get("last_status"):
        age = log.get("last_status_age_sec", "?")
        print(f"  Last status: {age}s ago")
        print(f"  Errors (1h): {log['errors_1h']} | Warnings: {log['warnings_1h']}")
        print(f"  Signals today: {log['signals_today']}")
    else:
        print("  No status lines found in log")

    # 5. Live status + system
    print("\n[5/5] Live Status & System")
    live = check_live_status()
    report["live_status"] = live
    if live.get("exists"):
        data = live.get("data", {})
        print(f"  live_status.json: exists (age: {live.get('age_seconds', '?')}s)")
        print(f"  Balance: ${data.get('balance', 'N/A')}")
        print(f"  Day P&L: ${data.get('day_pnl', 'N/A')}")
        print(f"  Trades today: {data.get('trades_today', 'N/A')}")
        print(f"  Locked: {data.get('locked', 'N/A')}")
        print(f"  Active symbols: {data.get('active_symbols', [])}")
    else:
        print("  live_status.json: NOT FOUND")

    sys_info = check_system()
    report["system"] = sys_info
    if sys_info:
        print(f"  Disk: {sys_info.get('disk_usage', '?')} | Memory: {sys_info.get('memory_pct', '?')}%")

    # Verdict
    bot_alive = proc["running"]
    token_ok = tok.get("valid", False)
    has_balance = bool(report.get("account", {}).get("balance"))
    log_fresh = (log.get("last_status_age_sec") or 9999) < 120
    live_fresh = live.get("exists") and (live.get("age_seconds") or 9999) < 120

    if bot_alive and token_ok and has_balance and (log_fresh or live_fresh):
        verdict = "HEALTHY"
    elif bot_alive and (token_ok or log_fresh):
        verdict = "DEGRADED"
    elif bot_alive:
        verdict = "RUNNING_NO_DATA"
    else:
        verdict = "DOWN"

    report["verdict"] = {
        "overall": verdict,
        "bot_alive": bot_alive,
        "token_valid": token_ok,
        "has_balance": has_balance,
        "log_fresh": log_fresh,
        "live_fresh": live_fresh,
    }

    print("\n" + "=" * 60)
    print(f"  VERDICT: {verdict}")
    print(f"  Bot: {'ALIVE' if bot_alive else 'DOWN'} | Token: {'OK' if token_ok else 'NO'} | "
          f"Balance: {'OK' if has_balance else 'NO'} | Log: {'FRESH' if log_fresh else 'STALE'}")
    print("=" * 60)

    # Write report
    STATUS_FILE.write_text(json.dumps(report, indent=2))
    print(f"\nReport: {STATUS_FILE}")

    return report


def main():
    if "--create-ping" in sys.argv:
        create_ping()
    else:
        run_health_check()


if __name__ == "__main__":
    main()
