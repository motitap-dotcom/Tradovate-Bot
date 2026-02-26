#!/usr/bin/env python3
"""
Remote Control API for Tradovate Bot
======================================
HTTP API that allows full remote management of the bot.
Runs alongside the bot as a background thread.

Endpoints:
    GET  /api/status      — Bot status, balance, positions, risk state
    GET  /api/log         — Recent log lines
    GET  /api/log/full    — Last 200 log lines
    GET  /api/journal     — Trade journal with analysis
    GET  /api/config      — Current configuration
    GET  /api/positions   — Open positions from API
    GET  /api/orders      — Working orders from API
    GET  /api/prices      — Current market prices
    GET  /api/health      — Simple health check
    POST /api/command     — Execute bot commands (stop, close-all, cancel-all, etc.)

Usage:
    # Standalone:
    python remote_control.py              # Start on port 8080
    python remote_control.py --port 5000  # Custom port

    # From bot.py (auto-started as background thread):
    from remote_control import start_remote_control
    start_remote_control(bot_instance, port=8080)
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import config

logger = logging.getLogger("remote_control")

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BOT_DIR, "bot.log")
TOKEN_FILE = os.path.join(BOT_DIR, ".tradovate_token.json")
JOURNAL_FILE = os.path.join(BOT_DIR, "trade_journal.json")
TUNER_LOG = os.path.join(BOT_DIR, "tuner_log.json")

# Global reference to bot instance (set when started from bot.py)
_bot_instance = None
_command_queue = []


class RemoteControlHandler(BaseHTTPRequestHandler):
    """HTTP request handler for bot remote control."""

    def log_message(self, format, *args):
        """Suppress default HTTP logging to avoid noise."""
        pass

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str, ensure_ascii=False).encode())

    def _send_text(self, text, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        routes = {
            "/api/health": self._handle_health,
            "/api/status": self._handle_status,
            "/api/log": self._handle_log,
            "/api/log/full": self._handle_log_full,
            "/api/journal": self._handle_journal,
            "/api/config": self._handle_config,
            "/api/positions": self._handle_positions,
            "/api/orders": self._handle_orders,
            "/api/prices": self._handle_prices,
            "/api/balance": self._handle_balance,
            "/api/token": self._handle_token,
            "/api/tuner": self._handle_tuner,
            "/api/summary": self._handle_summary,
        }

        handler = routes.get(path)
        if handler:
            try:
                handler(params)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "Not found", "available": list(routes.keys())}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                pass

        if path == "/api/command":
            self._handle_command(body)
        elif path == "/api/refresh-token":
            self._handle_refresh_token(body)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ─────────────────────────────────────────
    # GET handlers
    # ─────────────────────────────────────────

    def _handle_health(self, params):
        bot_running, pid = _is_bot_running()
        self._send_json({
            "status": "ok",
            "bot_running": bot_running,
            "pid": pid,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "uptime": _get_uptime(),
        })

    def _handle_status(self, params):
        bot_running, pid = _is_bot_running()
        token_info = _get_token_info()
        last_status = _get_last_status()
        api_balance = _get_api_balance() if token_info.get("status") == "valid" else None

        # Enrich with live API data
        if api_balance:
            last_status["balance"] = api_balance.get("totalCashValue", last_status.get("balance", 50000))
            last_status["unrealized_pnl"] = api_balance.get("openPnL", 0)
            net_liq = api_balance.get("netLiq", last_status.get("balance", 50000))
            last_status["net_liq"] = net_liq
            last_status["to_floor"] = net_liq - (50000 - 2500)
            last_status["api_source"] = "live"
        else:
            last_status["api_source"] = "log"

        self._send_json({
            "bot": {"running": bot_running, "pid": pid},
            "token": token_info,
            "trading": last_status,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "bot_connected": _bot_instance is not None,
        })

    def _handle_log(self, params):
        lines = int(params.get("lines", ["50"])[0])
        log_lines = _get_recent_log(min(lines, 200))
        self._send_json({
            "lines": log_lines,
            "count": len(log_lines),
            "log_file": LOG_FILE,
        })

    def _handle_log_full(self, params):
        log_lines = _get_recent_log(200)
        self._send_text("\n".join(log_lines))

    def _handle_journal(self, params):
        try:
            from trade_journal import TradeJournal
            journal = TradeJournal()
            summary = journal._compute_summary()
            by_symbol = journal.analyze_by_symbol()
            by_strategy = journal.analyze_by_strategy()
            by_hour = journal.analyze_by_hour()
            by_exit = journal.analyze_by_exit_reason()
            lessons = journal.generate_lessons()
            recent = journal._closed_trades()[-20:]
            open_trades = [t for t in journal.trades if t["status"] == "open"]

            self._send_json({
                "summary": summary,
                "by_symbol": by_symbol,
                "by_strategy": by_strategy,
                "by_hour": by_hour,
                "by_exit_reason": by_exit,
                "lessons": lessons,
                "recent_trades": recent,
                "open_trades": open_trades,
                "total_in_journal": len(journal.trades),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_config(self, params):
        challenge = config.ACTIVE_CHALLENGE
        specs = {}
        for sym, spec in config.CONTRACT_SPECS.items():
            specs[sym] = {
                "name": spec["name"],
                "strategy": spec["strategy"],
                "enabled": spec.get("enabled", False),
                "stop_loss": spec["stop_loss_points"],
                "take_profit": spec["take_profit_points"],
                "point_value": spec["point_value"],
                "tick_size": spec["tick_size"],
                "tick_value": spec["tick_value"],
            }
        self._send_json({
            "prop_firm": config.PROP_FIRM,
            "environment": config.ENVIRONMENT,
            "challenge": challenge,
            "contracts": specs,
            "risk_per_trade": config.RISK_PER_TRADE_PCT,
            "max_daily_trades": config.MAX_DAILY_TRADES,
            "trading_cutoff": config.TRADING_CUTOFF_ET,
            "force_close": config.FORCE_CLOSE_ET,
            "market_open": config.MARKET_OPEN_ET,
            "daily_loss_brake_pct": config.DAILY_LOSS_BRAKE_PCT,
        })

    def _handle_positions(self, params):
        positions = _get_positions()
        self._send_json({
            "positions": positions,
            "total_open": sum(abs(p.get("netPos", 0)) for p in positions),
        })

    def _handle_orders(self, params):
        orders = _get_orders()
        self._send_json({
            "orders": orders,
            "count": len(orders),
        })

    def _handle_prices(self, params):
        prices = _get_prices()
        self._send_json(prices)

    def _handle_balance(self, params):
        balance_data = _get_api_balance()
        if balance_data:
            self._send_json({
                "balance": balance_data.get("totalCashValue"),
                "net_liq": balance_data.get("netLiq"),
                "open_pnl": balance_data.get("openPnL"),
                "realized_pnl": balance_data.get("realizedPnL"),
                "raw": balance_data,
            })
        else:
            self._send_json({"error": "Could not fetch balance"}, 503)

    def _handle_token(self, params):
        self._send_json(_get_token_info())

    def _handle_tuner(self, params):
        if not os.path.exists(TUNER_LOG):
            self._send_json([])
            return
        try:
            with open(TUNER_LOG) as f:
                data = json.load(f)
            self._send_json(data[-20:])
        except Exception:
            self._send_json([])

    def _handle_summary(self, params):
        """One-call summary: everything Claude needs to assess the bot."""
        bot_running, pid = _is_bot_running()
        token_info = _get_token_info()
        last_status = _get_last_status()
        api_balance = _get_api_balance() if token_info.get("status") == "valid" else None
        log_lines = _get_recent_log(30)

        balance = 50000
        if api_balance:
            balance = api_balance.get("totalCashValue", 50000)

        try:
            from trade_journal import TradeJournal
            journal = TradeJournal()
            journal_summary = journal._compute_summary()
            today_trades = [t for t in journal.trades if t.get("date") == date.today().isoformat()]
            open_trades = [t for t in journal.trades if t["status"] == "open"]
        except Exception:
            journal_summary = {}
            today_trades = []
            open_trades = []

        self._send_json({
            "bot_running": bot_running,
            "pid": pid,
            "balance": balance,
            "api_balance": api_balance,
            "token": token_info,
            "last_status": last_status,
            "journal_summary": journal_summary,
            "today_trades": len(today_trades),
            "open_trades": len(open_trades),
            "recent_log": log_lines[-10:],
            "server_time": datetime.now(timezone.utc).isoformat(),
        })

    # ─────────────────────────────────────────
    # POST handlers
    # ─────────────────────────────────────────

    def _handle_command(self, body):
        cmd = body.get("command", "").lower().strip()

        commands = {
            "status": "Get bot status",
            "stop": "Stop the bot gracefully",
            "restart": "Restart the bot via systemd",
            "close-all": "Close all open positions",
            "cancel-all": "Cancel all working orders",
            "log": "Get recent log lines",
            "journal": "Get trade journal summary",
            "refresh-token": "Refresh the auth token",
        }

        if not cmd:
            self._send_json({
                "error": "No command specified",
                "available_commands": commands,
            }, 400)
            return

        if cmd == "status":
            bot_running, pid = _is_bot_running()
            self._send_json({
                "command": cmd,
                "result": "ok",
                "bot_running": bot_running,
                "pid": pid,
            })

        elif cmd == "stop":
            result = _run_systemctl("stop")
            self._send_json({"command": cmd, "result": result})

        elif cmd == "restart":
            result = _run_systemctl("restart")
            self._send_json({"command": cmd, "result": result})

        elif cmd == "close-all":
            result = _close_all_positions()
            self._send_json({"command": cmd, "result": result})

        elif cmd == "cancel-all":
            result = _cancel_all_orders()
            self._send_json({"command": cmd, "result": result})

        elif cmd == "refresh-token":
            result = _refresh_token()
            self._send_json({"command": cmd, "result": result})

        else:
            self._send_json({
                "error": f"Unknown command: {cmd}",
                "available_commands": commands,
            }, 400)

    def _handle_refresh_token(self, body):
        result = _refresh_token()
        self._send_json({"result": result, "token": _get_token_info()})


# ─────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────

def _is_bot_running():
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "bot.py"], text=True, stderr=subprocess.DEVNULL
        )
        pids = [int(p) for p in out.strip().split("\n") if p]
        return True, pids[0] if pids else 0
    except subprocess.CalledProcessError:
        return False, 0


def _get_uptime():
    try:
        out = subprocess.check_output(
            ["systemctl", "show", "tradovate-bot", "--property=ActiveEnterTimestamp"],
            text=True, stderr=subprocess.DEVNULL,
        )
        ts_str = out.strip().split("=", 1)[-1]
        if ts_str:
            return ts_str
    except Exception:
        pass
    return "unknown"


def _get_last_status():
    if not os.path.exists(LOG_FILE):
        return {}
    try:
        import re
        out = subprocess.check_output(
            ["tail", "-100", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return {}

    import re
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Status \| "
        r"balance=([\d.]+) \| day_pnl=([-\d.]+) \| to_floor=([-\d.]+) \| "
        r"contracts=(\d+)/(\d+) \| trades=(\d+)/(\d+) \| locked=(\w+)"
    )
    for line in reversed(out.strip().split("\n")):
        m = pattern.search(line)
        if m:
            return {
                "timestamp": m.group(1),
                "balance": float(m.group(2)),
                "day_pnl": float(m.group(3)),
                "to_floor": float(m.group(4)),
                "open_contracts": int(m.group(5)),
                "max_contracts": int(m.group(6)),
                "trades_today": int(m.group(7)),
                "max_trades": int(m.group(8)),
                "locked": m.group(9) == "True",
            }
    return {}


def _get_token_info():
    if not os.path.exists(TOKEN_FILE):
        return {"status": "missing", "minutes_remaining": 0}
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        exp = data.get("expirationTime", "")
        if not exp:
            return {"status": "unknown", "minutes_remaining": 0}
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
        return {
            "status": "valid" if remaining > 0 else "expired",
            "minutes_remaining": max(0, round(remaining)),
            "expires_at": exp,
        }
    except Exception:
        return {"status": "error", "minutes_remaining": 0}


def _get_recent_log(lines=50):
    if not os.path.exists(LOG_FILE):
        # Try journalctl as fallback
        try:
            out = subprocess.check_output(
                ["journalctl", "-u", "tradovate-bot", "--no-pager", "-n", str(lines)],
                text=True, stderr=subprocess.DEVNULL,
            )
            return out.strip().split("\n")
        except Exception:
            return ["No log file found"]
    try:
        out = subprocess.check_output(
            ["tail", f"-{lines}", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip().split("\n")
    except subprocess.CalledProcessError:
        return []


def _get_api_balance():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        import requests
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        account_id = t.get("accountId")
        if not token or not account_id:
            return None
        resp = requests.post(
            f"{config.REST_URL}/cashBalance/getcashbalancesnapshot",
            json={"accountId": account_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("errorText"):
                return data
    except Exception:
        pass
    return None


def _get_positions():
    if not os.path.exists(TOKEN_FILE):
        return []
    try:
        import requests
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        if not token:
            return []
        resp = requests.get(
            f"{config.REST_URL}/position/list",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


def _get_orders():
    if not os.path.exists(TOKEN_FILE):
        return []
    try:
        import requests
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        if not token:
            return []
        resp = requests.get(
            f"{config.REST_URL}/order/list",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


def _get_prices():
    try:
        import requests
        symbols = {"NQ": "NQ=F", "ES": "ES=F", "GC": "GC=F", "CL": "CL=F"}
        prices = {}
        for name, sym in symbols.items():
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                if resp.status_code == 200:
                    meta = resp.json()["chart"]["result"][0]["meta"]
                    prices[name] = {
                        "price": meta.get("regularMarketPrice", 0),
                        "high": meta.get("regularMarketDayHigh", 0),
                        "low": meta.get("regularMarketDayLow", 0),
                        "change": round(meta.get("regularMarketPrice", 0) - meta.get("chartPreviousClose", 0), 4),
                    }
            except Exception:
                pass
        return prices
    except Exception:
        return {}


def _run_systemctl(action):
    try:
        subprocess.check_output(
            ["systemctl", action, "tradovate-bot"],
            text=True, stderr=subprocess.STDOUT, timeout=30,
        )
        return f"tradovate-bot {action} OK"
    except subprocess.CalledProcessError as e:
        return f"systemctl {action} failed: {e.output}"
    except Exception as e:
        return f"systemctl {action} error: {e}"


def _close_all_positions():
    if not os.path.exists(TOKEN_FILE):
        return "No token file"
    try:
        import requests
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        account_id = t.get("accountId")
        if not token or not account_id:
            return "No valid token"

        headers = {"Authorization": f"Bearer {token}"}

        # Get positions
        resp = requests.get(f"{config.REST_URL}/position/list", headers=headers, timeout=10)
        if resp.status_code != 200:
            return f"Failed to get positions: {resp.status_code}"

        positions = resp.json()
        closed = 0
        for p in positions:
            net = p.get("netPos", 0)
            if net == 0:
                continue
            contract_id = p.get("contractId")
            action = "Sell" if net > 0 else "Buy"
            qty = abs(net)

            order = {
                "accountSpec": t.get("accountSpec", ""),
                "accountId": account_id,
                "action": action,
                "symbol": str(contract_id),
                "orderQty": qty,
                "orderType": "Market",
                "isAutomated": True,
            }
            resp = requests.post(
                f"{config.REST_URL}/order/placeorder",
                json=order,
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                closed += 1

        return f"Closed {closed} positions"
    except Exception as e:
        return f"Error: {e}"


def _cancel_all_orders():
    if not os.path.exists(TOKEN_FILE):
        return "No token file"
    try:
        import requests
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        token = t.get("accessToken", "")
        account_id = t.get("accountId")
        if not token or not account_id:
            return "No valid token"

        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(f"{config.REST_URL}/order/list", headers=headers, timeout=10)
        if resp.status_code != 200:
            return f"Failed to get orders: {resp.status_code}"

        orders = resp.json()
        cancelled = 0
        for o in orders:
            status = o.get("ordStatus", "")
            if status in ("Working", "Accepted"):
                resp = requests.post(
                    f"{config.REST_URL}/order/cancelorder",
                    json={"orderId": o["id"]},
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    cancelled += 1

        return f"Cancelled {cancelled} orders"
    except Exception as e:
        return f"Error: {e}"


def _refresh_token():
    try:
        from tradovate_api import TradovateAPI
        api = TradovateAPI()
        if api.authenticate():
            return "Token refreshed successfully"
        return "Authentication failed"
    except Exception as e:
        return f"Error: {e}"


# ─────────────────────────────────────────
# Server startup
# ─────────────────────────────────────────

def start_remote_control(bot=None, port=8080):
    """Start the remote control API as a background daemon thread."""
    global _bot_instance
    _bot_instance = bot

    def _run():
        server = HTTPServer(("0.0.0.0", port), RemoteControlHandler)
        logger.info("Remote control API running on port %d", port)
        server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True, name="remote-control")
    thread.start()
    return thread


def main():
    """Standalone mode."""
    import argparse
    parser = argparse.ArgumentParser(description="Tradovate Bot Remote Control API")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print(f"\n  Remote Control API running at: http://0.0.0.0:{args.port}")
    print(f"  Endpoints:")
    print(f"    GET  /api/health    — Health check")
    print(f"    GET  /api/status    — Full bot status")
    print(f"    GET  /api/summary   — Everything in one call")
    print(f"    GET  /api/log       — Recent logs")
    print(f"    GET  /api/journal   — Trade journal")
    print(f"    GET  /api/config    — Configuration")
    print(f"    GET  /api/positions — Open positions")
    print(f"    GET  /api/orders    — Working orders")
    print(f"    GET  /api/prices    — Market prices")
    print(f"    GET  /api/balance   — Account balance")
    print(f"    POST /api/command   — Execute commands")
    print(f"  Press Ctrl+C to stop\n")

    server = HTTPServer(("0.0.0.0", args.port), RemoteControlHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
