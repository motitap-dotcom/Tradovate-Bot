#!/usr/bin/env python3
"""
Server Connection Verifier & Live Listener
============================================
Connects to the Tradovate server, verifies authentication,
retrieves account data, and listens to live market data via WebSocket.

Runs ON THE SERVER (called by server_cron.sh or manually).
Writes results to connect_status.json.

Usage:
    python connect_verify.py              # Full check + WS listen
    python connect_verify.py --quick      # API check only (no WS)
    python connect_verify.py --listen 30  # Listen to WS for 30 seconds
"""

import json
import os
import re
import subprocess
import sys
import time
import threading
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
STATUS_FILE = BOT_DIR / "connect_status.json"

DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"
WS_MD_URL = "wss://md-demo.tradovateapi.com/v1/websocket"
WS_TRADING_URL = "wss://demo.tradovateapi.com/v1/websocket"
ACCOUNT_ID = 39996695


# ── Helpers ──

def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _age_str(seconds):
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    return f"{seconds/3600:.1f}h"


# ── 1. Bot Process Check ──

def check_bot_process():
    """Check if bot.py is running via systemd or as a process."""
    result = {
        "running": False,
        "pid": None,
        "service_status": "unknown",
        "uptime_since": None,
        "uptime_minutes": None,
    }

    # systemd
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
        except Exception:
            pass
    else:
        # Fallback: pgrep
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


# ── 2. Token Check ──

def check_token():
    """Load and validate the saved token."""
    result = {
        "exists": False,
        "valid": False,
        "expired": False,
        "minutes_remaining": 0,
        "token": None,
    }

    if not TOKEN_FILE.exists():
        return result

    try:
        data = json.loads(TOKEN_FILE.read_text())
        result["exists"] = True
        token = data.get("accessToken", "")
        exp = data.get("expirationTime", "")

        if exp:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
            result["minutes_remaining"] = round(remaining, 1)
            result["expired"] = remaining <= 0

        if token and not result["expired"]:
            try:
                r = requests.get(
                    f"{DEMO_URL}/account/list",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if r.status_code == 200:
                    result["valid"] = True
                    result["token"] = token
                    result["md_token"] = data.get("mdAccessToken")
            except Exception:
                pass

    except Exception as e:
        result["error"] = str(e)

    return result


# ── 3. Account Check ──

def check_account(token):
    """Get account balance, positions, orders."""
    result = {
        "balance": None,
        "net_liq": None,
        "realized_pnl": None,
        "unrealized_pnl": None,
        "open_positions": 0,
        "active_orders": 0,
        "account_name": None,
        "account_active": None,
        "positions_detail": [],
        "orders_detail": [],
    }
    headers = {"Authorization": f"Bearer {token}"}

    # Account info
    try:
        r = requests.get(f"{DEMO_URL}/account/list", headers=headers, timeout=10)
        if r.status_code == 200:
            for a in r.json():
                if a.get("id") == ACCOUNT_ID or len(r.json()) == 1:
                    result["account_name"] = a.get("name")
                    result["account_active"] = a.get("active")
    except Exception:
        pass

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
                {"contractId": p.get("contractId"), "netPos": p.get("netPos"),
                 "netPrice": p.get("netPrice")}
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
                {"id": o.get("id"), "action": o.get("action"),
                 "qty": o.get("orderQty"), "status": o.get("ordStatus"),
                 "contractId": o.get("contractId")}
                for o in active
            ]
    except Exception:
        pass

    return result


# ── 4. WebSocket Market Data Listener ──

def listen_market_data(token, listen_seconds=15):
    """
    Connect to Tradovate market data WebSocket, authenticate,
    subscribe to quotes, and listen for live data.
    """
    result = {
        "ws_connected": False,
        "ws_auth_ok": False,
        "ws_latency_ms": None,
        "quotes_received": 0,
        "symbols_heard": [],
        "last_quotes": {},
        "listen_seconds": listen_seconds,
    }

    try:
        import websocket as ws_lib
    except ImportError:
        result["error"] = "websocket-client not installed"
        return result

    quotes_lock = threading.Lock()
    done_event = threading.Event()

    def on_message(ws, message):
        nonlocal result
        # Tradovate WS protocol: "endpoint\nid\n{json}"
        if not message or message.startswith("o"):
            return

        parts = message.split("\n", 2)
        if len(parts) >= 3:
            endpoint = parts[0].strip()
            try:
                data = json.loads(parts[2])
            except (json.JSONDecodeError, IndexError):
                return

            # Auth response
            if endpoint == "authorize" or "authorize" in message:
                if data.get("s") == 200:
                    result["ws_auth_ok"] = True

            # Quote data
            if "d" in data and isinstance(data["d"], dict):
                with quotes_lock:
                    result["quotes_received"] += 1
                    contract_id = data["d"].get("contractId")
                    if contract_id:
                        result["last_quotes"][str(contract_id)] = {
                            "bid": data["d"].get("bid", {}).get("price"),
                            "ask": data["d"].get("ask", {}).get("price"),
                            "last": data["d"].get("trade", {}).get("price"),
                            "time": _ts(),
                        }
                        if str(contract_id) not in result["symbols_heard"]:
                            result["symbols_heard"].append(str(contract_id))

    def on_error(ws, error):
        result["ws_error"] = str(error)

    def on_open(ws):
        result["ws_connected"] = True
        # Send auth
        auth_payload = json.dumps({"token": token})
        ws.send(f"authorize\n1\n{auth_payload}")

        # Subscribe to key contracts after a short delay
        def subscribe():
            time.sleep(2)
            # Subscribe to NQ, ES, GC, CL front-month contracts
            for i, symbol in enumerate(["NQ", "ES", "GC", "CL"], start=2):
                sub_msg = f"md/subscribequote\n{i}\n" + json.dumps({"symbol": symbol})
                try:
                    ws.send(sub_msg)
                except Exception:
                    break
                time.sleep(0.5)

        threading.Thread(target=subscribe, daemon=True).start()

    t0 = time.time()
    ws = ws_lib.WebSocketApp(
        WS_MD_URL,
        on_message=on_message,
        on_error=on_error,
        on_open=on_open,
    )

    # Run WebSocket in a thread with timeout
    ws_thread = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 10})
    ws_thread.daemon = True
    ws_thread.start()

    # Wait for auth
    deadline = time.time() + 10
    while time.time() < deadline and not result["ws_auth_ok"]:
        time.sleep(0.5)

    if result["ws_connected"]:
        result["ws_latency_ms"] = int((time.time() - t0) * 1000)

    # Listen for quotes
    if result["ws_auth_ok"]:
        time.sleep(listen_seconds)

    # Close WebSocket
    try:
        ws.close()
    except Exception:
        pass

    ws_thread.join(timeout=5)
    return result


# ── 5. Trading WebSocket Check ──

def check_trading_ws(token):
    """Quick connect test to trading WebSocket."""
    result = {"connected": False, "auth_ok": False, "latency_ms": None}

    try:
        import websocket as ws_lib
        t0 = time.time()
        ws = ws_lib.create_connection(WS_TRADING_URL, timeout=10)

        welcome = ws.recv()
        if welcome:
            result["connected"] = True

        auth_msg = f"authorize\n1\n{json.dumps({'token': token})}"
        ws.send(auth_msg)
        auth_resp = ws.recv()

        result["latency_ms"] = int((time.time() - t0) * 1000)

        if auth_resp and '"s":200' in auth_resp:
            result["auth_ok"] = True

        ws.close()
    except ImportError:
        result["error"] = "websocket-client not installed"
    except Exception as e:
        result["error"] = str(e)

    return result


# ── 6. Bot Log Analysis ──

def check_bot_log():
    """Analyze bot.log for health indicators."""
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
        with open(LOG_FILE) as f:
            lines = f.readlines()

        tail = lines[-200:] if len(lines) > 200 else lines
        result["last_lines"] = [l.strip() for l in tail[-10:]]

        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        for line in reversed(tail):
            line = line.strip()

            if "Status |" in line and result["last_status_line"] is None:
                result["last_status_line"] = line
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    ts = ts.replace(tzinfo=timezone.utc)
                    age = (now - ts).total_seconds()
                    result["last_status_age_seconds"] = int(age)
                except Exception:
                    pass

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


# ── 7. Live Status File ──

def check_live_status():
    """Read bot's live_status.json."""
    if not LIVE_STATUS_FILE.exists():
        return {"exists": False}

    try:
        data = json.loads(LIVE_STATUS_FILE.read_text())
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


# ── 8. System Resources ──

def check_system():
    """Get disk, memory, load."""
    result = {}

    try:
        disk = subprocess.check_output(["df", "-h", "/"], text=True, stderr=subprocess.DEVNULL)
        parts = disk.strip().split("\n")[-1].split()
        result["disk_usage"] = parts[4] if len(parts) > 4 else "?"
        result["disk_available"] = parts[3] if len(parts) > 3 else "?"
    except Exception:
        pass

    try:
        mem = subprocess.check_output(["free", "-m"], text=True, stderr=subprocess.DEVNULL)
        mem_parts = mem.strip().split("\n")[1].split()
        result["memory_used_mb"] = int(mem_parts[2])
        result["memory_total_mb"] = int(mem_parts[1])
        result["memory_pct"] = round(int(mem_parts[2]) / int(mem_parts[1]) * 100, 1)
    except Exception:
        pass

    try:
        load = subprocess.check_output(["cat", "/proc/loadavg"], text=True, stderr=subprocess.DEVNULL)
        result["load_avg"] = load.strip().split()[:3]
    except Exception:
        pass

    # Git info
    try:
        result["git_commit"] = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %s"],
            text=True, stderr=subprocess.DEVNULL, cwd=str(BOT_DIR),
        ).strip()
        result["git_branch"] = subprocess.check_output(
            ["git", "branch", "--show-current"],
            text=True, stderr=subprocess.DEVNULL, cwd=str(BOT_DIR),
        ).strip()
    except Exception:
        pass

    return result


# ── Main ──

def main():
    quick = "--quick" in sys.argv
    listen_time = 15  # default

    for i, arg in enumerate(sys.argv):
        if arg == "--listen" and i + 1 < len(sys.argv):
            try:
                listen_time = int(sys.argv[i + 1])
            except ValueError:
                pass

    print("=" * 60)
    print("  TRADOVATE BOT — CONNECTION VERIFIER & LISTENER")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "verdict": {},
    }

    # ── 1. Bot Process ──
    print(f"\n[{_ts()}] 1/7 Bot Process")
    proc = check_bot_process()
    report["checks"]["process"] = proc
    status_icon = "RUNNING" if proc["running"] else "STOPPED"
    print(f"  Service: {proc['service_status']} | PID: {proc.get('pid', '-')}")
    print(f"  Status: {status_icon}")
    if proc.get("uptime_since"):
        print(f"  Up since: {proc['uptime_since']}")

    # ── 2. Token ──
    print(f"\n[{_ts()}] 2/7 Auth Token")
    tok = check_token()
    report["checks"]["token"] = {k: v for k, v in tok.items() if k not in ("token", "md_token")}
    if tok["valid"]:
        print(f"  Token: VALID ({tok['minutes_remaining']}m remaining)")
    elif tok["exists"]:
        print(f"  Token: {'EXPIRED' if tok['expired'] else 'INVALID'}")
    else:
        print("  Token: NOT FOUND")

    token = tok.get("token")
    md_token = tok.get("md_token")

    # ── 3. Account ──
    print(f"\n[{_ts()}] 3/7 Account Status")
    if token:
        acct = check_account(token)
        report["checks"]["account"] = acct
        print(f"  Account: {acct.get('account_name', 'N/A')} (active={acct.get('account_active')})")
        print(f"  Balance:     ${acct.get('balance', 'N/A'):>12}")
        print(f"  Net Liq:     ${acct.get('net_liq', 'N/A'):>12}")
        print(f"  Realized:    ${acct.get('realized_pnl', 'N/A'):>12}")
        print(f"  Unrealized:  ${acct.get('unrealized_pnl', 'N/A'):>12}")
        print(f"  Positions:   {acct['open_positions']}")
        print(f"  Orders:      {acct['active_orders']}")

        if acct["positions_detail"]:
            print("  Open positions:")
            for p in acct["positions_detail"]:
                print(f"    contractId={p['contractId']} qty={p['netPos']} price={p['netPrice']}")
    else:
        report["checks"]["account"] = {"skipped": True, "reason": "no_valid_token"}
        print("  Skipped (no valid token)")

    # ── 4. Trading WebSocket ──
    print(f"\n[{_ts()}] 4/7 Trading WebSocket")
    if token and not quick:
        trading_ws = check_trading_ws(token)
        report["checks"]["trading_ws"] = trading_ws
        print(f"  Connected: {trading_ws['connected']} | Auth: {trading_ws['auth_ok']} | Latency: {trading_ws.get('latency_ms', '?')}ms")
    else:
        report["checks"]["trading_ws"] = {"skipped": True}
        print(f"  Skipped ({'quick mode' if quick else 'no token'})")

    # ── 5. Market Data WebSocket (Listen) ──
    print(f"\n[{_ts()}] 5/7 Market Data WebSocket")
    if (md_token or token) and not quick:
        ws_token = md_token or token
        print(f"  Connecting and listening for {listen_time}s...")
        md_result = listen_market_data(ws_token, listen_seconds=listen_time)
        report["checks"]["market_data_ws"] = md_result
        print(f"  Connected: {md_result['ws_connected']} | Auth: {md_result['ws_auth_ok']}")
        print(f"  Latency: {md_result.get('ws_latency_ms', '?')}ms")
        print(f"  Quotes received: {md_result['quotes_received']}")
        if md_result["last_quotes"]:
            print(f"  Symbols heard: {len(md_result['symbols_heard'])}")
            for cid, q in md_result["last_quotes"].items():
                print(f"    Contract {cid}: bid={q.get('bid')} ask={q.get('ask')} last={q.get('last')}")
        if md_result.get("ws_error"):
            print(f"  Error: {md_result['ws_error']}")
    else:
        report["checks"]["market_data_ws"] = {"skipped": True}
        print(f"  Skipped ({'quick mode' if quick else 'no token'})")

    # ── 6. Bot Log ──
    print(f"\n[{_ts()}] 6/7 Bot Log Analysis")
    log = check_bot_log()
    report["checks"]["log"] = {k: v for k, v in log.items() if k != "last_lines"}
    print(f"  Log exists: {log['log_exists']}")
    if log.get("last_status_line"):
        age = log.get("last_status_age_seconds", 0)
        print(f"  Last status: {_age_str(age)} ago")
    print(f"  Errors (1h): {log['errors_last_hour']} | Warnings: {log['warnings_last_hour']}")
    print(f"  Signals: {log['signals_today']} | Trades: {log['trades_today']}")
    if log.get("last_lines"):
        print("  Last log lines:")
        for line in log["last_lines"][-5:]:
            print(f"    {line[:80]}")

    # ── 7. Live Status + System ──
    print(f"\n[{_ts()}] 7/7 System & Live Status")
    live = check_live_status()
    report["checks"]["live_status"] = {k: v for k, v in live.items() if k != "data"}
    if live.get("exists"):
        age = live.get("age_seconds")
        print(f"  live_status.json: exists ({_age_str(age)} ago)")
        data = live.get("data", {})
        if data:
            print(f"    Balance:   ${data.get('balance', 'N/A')}")
            print(f"    Day P&L:   ${data.get('day_pnl', 'N/A')}")
            print(f"    To floor:  ${data.get('distance_to_floor', 'N/A')}")
            print(f"    Contracts: {data.get('open_contracts', 'N/A')}")
            print(f"    Locked:    {data.get('locked', 'N/A')}")
            print(f"    Symbols:   {', '.join(data.get('active_symbols', []))}")
    else:
        print("  live_status.json: not found")

    system = check_system()
    report["checks"]["system"] = system
    if system:
        print(f"  Disk: {system.get('disk_usage', '?')} | Memory: {system.get('memory_pct', '?')}%")
        if system.get("git_commit"):
            print(f"  Git: {system['git_commit']}")

    # ── Verdict ──
    bot_alive = proc["running"]
    token_ok = tok["valid"]
    has_balance = bool(report["checks"].get("account", {}).get("balance"))
    trading_ws_ok = report["checks"].get("trading_ws", {}).get("auth_ok", False)
    md_ws_ok = report["checks"].get("market_data_ws", {}).get("ws_auth_ok", False)
    log_fresh = (log.get("last_status_age_seconds") or 9999) < 120
    live_fresh = (live.get("age_seconds") or 9999) < 120

    checks_passed = sum([bot_alive, token_ok, has_balance, log_fresh])

    if checks_passed >= 4:
        overall = "HEALTHY"
    elif checks_passed >= 2:
        overall = "DEGRADED"
    else:
        overall = "DOWN"

    report["verdict"] = {
        "bot_alive": bot_alive,
        "token_valid": token_ok,
        "has_balance_data": has_balance,
        "trading_ws": trading_ws_ok or "skipped",
        "market_data_ws": md_ws_ok or "skipped",
        "log_fresh": log_fresh,
        "live_status_fresh": live_fresh,
        "overall": overall,
    }

    # Include live status data in report for external monitoring
    if live.get("data"):
        report["live_bot_status"] = live["data"]
    if report["checks"].get("account", {}).get("balance"):
        report["account_snapshot"] = {
            "balance": report["checks"]["account"]["balance"],
            "net_liq": report["checks"]["account"]["net_liq"],
            "realized_pnl": report["checks"]["account"]["realized_pnl"],
            "unrealized_pnl": report["checks"]["account"]["unrealized_pnl"],
            "open_positions": report["checks"]["account"]["open_positions"],
            "active_orders": report["checks"]["account"]["active_orders"],
        }

    print("\n" + "=" * 60)
    print("  VERDICT")
    print("=" * 60)
    print(f"  Bot Alive:       {'YES' if bot_alive else 'NO'}")
    print(f"  Token Valid:     {'YES' if token_ok else 'NO'}")
    print(f"  Balance Data:    {'YES' if has_balance else 'NO'}")
    print(f"  Trading WS:     {'YES' if trading_ws_ok else 'SKIP' if quick else 'NO'}")
    print(f"  Market Data WS:  {'YES' if md_ws_ok else 'SKIP' if quick else 'NO'}")
    print(f"  Log Fresh:       {'YES' if log_fresh else 'NO'}")
    print(f"  Live Status:     {'YES' if live_fresh else 'NO'}")
    print(f"  Overall:         {overall}")
    print("=" * 60)

    # Write report
    with open(STATUS_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport: {STATUS_FILE}")

    return 0 if overall in ("HEALTHY", "DEGRADED") else 1


if __name__ == "__main__":
    sys.exit(main())
