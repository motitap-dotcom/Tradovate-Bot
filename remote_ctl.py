#!/usr/bin/env python3
"""
Remote Control Client for Tradovate Bot
==========================================
Communicates with the Management API running on the VPS.
Designed to work from Claude Code web sessions.

Setup:
    Set these environment variables (or put in .env):
        VPS_URL=http://<your-vps-ip>:9090
        MGMT_API_KEY=<your-api-key>

Usage:
    python remote_ctl.py status        # Full bot status
    python remote_ctl.py logs          # Last 50 log lines
    python remote_ctl.py logs 100      # Last 100 log lines
    python remote_ctl.py activity      # Recent signals/trades
    python remote_ctl.py journal       # Trade journal summary
    python remote_ctl.py trades        # All trades
    python remote_ctl.py token         # Token status
    python remote_ctl.py start         # Start the bot
    python remote_ctl.py stop          # Stop the bot
    python remote_ctl.py restart       # Restart the bot
    python remote_ctl.py update        # Git pull + restart
    python remote_ctl.py ping          # Health check
"""

import json
import os
import sys
import urllib.request
import urllib.error

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BOT_DIR, ".env")

# ── Load config ──────────────────────────────────────────


def _load_env():
    """Load .env file if it exists."""
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


_env = _load_env()
VPS_URL = (os.environ.get("VPS_URL") or _env.get("VPS_URL", "")).rstrip("/")
API_KEY = os.environ.get("MGMT_API_KEY") or _env.get("MGMT_API_KEY", "")


# ── API client ───────────────────────────────────────────


def api_call(method, path, timeout=15):
    """Make an HTTP request to the management API."""
    if not VPS_URL:
        print("ERROR: VPS_URL not set. Add VPS_URL=http://<ip>:9090 to .env")
        sys.exit(1)
    if not API_KEY:
        print("ERROR: MGMT_API_KEY not set. Add it to .env")
        sys.exit(1)

    url = f"{VPS_URL}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("X-API-Key", API_KEY)
    req.add_header("Accept", "application/json")

    if method == "POST":
        req.add_header("Content-Type", "application/json")
        req.data = b"{}"

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            err = json.loads(body)
        except Exception:
            err = {"error": body or str(e)}
        print(f"HTTP {e.code}: {err.get('error', body)}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection failed: {e.reason}")
        print(f"URL: {url}")
        print("Is the management API running on your VPS?")
        sys.exit(1)


# ── Display helpers ──────────────────────────────────────

G = "\033[32m"
R = "\033[31m"
Y = "\033[33m"
C = "\033[36m"
B = "\033[1m"
D = "\033[2m"
X = "\033[0m"


def _pnl(val):
    s = "+" if val >= 0 else ""
    c = G if val >= 0 else R
    return f"{c}{s}${val:,.2f}{X}"


def show_status(data):
    """Pretty-print full status."""
    bot = data["bot"]
    trading = data["trading"]
    token = data["token"]
    journal = data.get("journal", {})

    print(f"{B}{'=' * 55}{X}")
    print(f"{B}  TRADOVATE BOT — REMOTE STATUS{X}")
    print(f"{'=' * 55}")

    # Bot status
    if bot["running"]:
        print(f"  Bot:     {G}RUNNING{X} (PID {bot['pid']}) | systemd: {bot['systemd']}")
    else:
        print(f"  Bot:     {R}STOPPED{X} | systemd: {bot['systemd']}")

    # Token
    tok = token.get("status", "unknown")
    mins = token.get("remaining_minutes", 0)
    if tok == "valid":
        print(f"  Token:   {G}OK{X} ({mins:.0f} min remaining)")
    elif tok == "expired":
        print(f"  Token:   {R}EXPIRED{X}")
    else:
        print(f"  Token:   {Y}{tok}{X}")

    # Data freshness
    age = trading.get("age_seconds", -1)
    if age >= 0:
        if age < 60:
            fresh = f"{age:.0f}s ago"
        elif age < 3600:
            fresh = f"{age/60:.0f}m ago"
        else:
            fresh = f"{R}stale ({age/3600:.1f}h){X}"
        print(f"  Data:    {fresh}")

    # Balance
    print(f"{'-' * 55}")
    balance = trading.get("balance", 0)
    day_pnl = trading.get("day_pnl", 0)
    to_floor = trading.get("to_floor", 0)

    print(f"  {B}Balance:{X}    ${balance:>12,.2f}")
    print(f"  Day P&L:    {_pnl(day_pnl)}")

    floor_c = R if to_floor < 500 else Y if to_floor < 1000 else G
    print(f"  To Floor:   {floor_c}${to_floor:>12,.2f}{X}")

    # Progress
    profit = balance - 50000
    target = 5000
    pct = max(0, min(100, (profit / target) * 100)) if target else 0
    bar_w = 30
    filled = int(pct / 100 * bar_w)
    bar = "█" * filled + "░" * (bar_w - filled)
    bar_c = G if pct > 0 else R
    print(f"  Progress:   {bar_c}{bar}{X} {pct:.1f}%")

    # Trading
    print(f"{'-' * 55}")
    contracts = trading.get("contracts", "0/0")
    trades = trading.get("trades", "0/0")
    locked = trading.get("locked", False)
    lock_str = f"  | {R}{B}LOCKED{X}" if locked else ""
    print(f"  Contracts: {contracts}  |  Trades: {trades}{lock_str}")

    # Journal
    total_trades = journal.get("total_trades", 0)
    if total_trades > 0:
        wr = journal.get("win_rate", 0)
        wins = journal.get("wins", 0)
        losses = journal.get("losses", 0)
        total_pnl = journal.get("total_pnl", 0)
        wr_c = G if wr >= 0.5 else Y if wr >= 0.4 else R
        print(f"{'-' * 55}")
        print(f"  Journal:    {total_trades} trades | {wr_c}{wr:.0%} WR{X} ({wins}W/{losses}L)")
        print(f"  Total P&L:  {_pnl(total_pnl)}")

    print(f"{'=' * 55}")
    print(f"  {D}{data.get('server_time', '')}{X}")


def show_logs(data):
    """Print log lines."""
    lines = data.get("lines", [])
    if not lines:
        print("No log lines available.")
        return
    for line in lines:
        # Color code important lines
        if "ERROR" in line or "LOCKED" in line:
            print(f"{R}{line}{X}")
        elif "SIGNAL:" in line or "ENTRY" in line:
            print(f"{G}{line}{X}")
        elif "WARNING" in line:
            print(f"{Y}{line}{X}")
        else:
            print(line)


def show_activity(data):
    """Print recent activity."""
    items = data.get("activity", [])
    if not items:
        print("No recent activity.")
        return
    print(f"{B}Recent Activity:{X}")
    for line in items:
        if "SIGNAL:" in line or "ENTRY" in line:
            print(f"  {G}>{X} {line}")
        elif "LOCKED" in line or "DRAWDOWN" in line or "DAILY LOSS" in line:
            print(f"  {R}!{X} {line}")
        elif "EXIT" in line:
            print(f"  {Y}${X} {line}")
        else:
            print(f"  {D} {line}{X}")


def show_journal(data):
    """Print journal summary."""
    s = data.get("summary", {})
    total = s.get("total_trades", 0)
    if total == 0:
        print("No trades in journal yet.")
        return
    print(f"{B}Trade Journal Summary:{X}")
    print(f"  Total trades:   {total}")
    print(f"  Wins/Losses:    {s.get('wins', 0)}/{s.get('losses', 0)}")
    print(f"  Win rate:       {s.get('win_rate', 0):.1%}")
    print(f"  Total P&L:      {_pnl(s.get('total_pnl', 0))}")
    print(f"  Profit factor:  {s.get('profit_factor', 0):.2f}")
    print(f"  Expectancy:     {_pnl(s.get('expectancy', 0))}/trade")


def show_trades(data):
    """Print trades list."""
    trades = data.get("trades", [])
    if not trades:
        print("No trades recorded.")
        return
    print(f"{B}{'Date':<12} {'Symbol':<8} {'Dir':<6} {'Qty':>4} {'P&L':>12} {'Status':<8}{X}")
    print("-" * 55)
    for t in trades:
        pnl = t.get("pnl")
        pnl_str = _pnl(pnl) if pnl is not None else f"{D}open{X}"
        d = t.get("entry_time", "")[:10]
        print(f"  {d:<12} {t.get('symbol', '?'):<8} {t.get('direction', '?'):<6} "
              f"{t.get('quantity', 0):>4} {pnl_str:>22} {t.get('status', '?'):<8}")


def show_action_result(data, action):
    """Print result of a bot action."""
    ok = data.get("ok", False)
    msg = data.get("message", "")
    if ok:
        print(f"{G}OK{X}: {msg}")
    else:
        print(f"{R}FAILED{X}: {msg}")


# ── Main ─────────────────────────────────────────────────


COMMANDS = {
    "status":   ("GET",  "/status",          show_status),
    "logs":     ("GET",  "/logs",            show_logs),
    "activity": ("GET",  "/logs/activity",   show_activity),
    "journal":  ("GET",  "/journal",         show_journal),
    "trades":   ("GET",  "/journal/trades",  show_trades),
    "token":    ("GET",  "/token",           None),
    "start":    ("POST", "/bot/start",       None),
    "stop":     ("POST", "/bot/stop",        None),
    "restart":  ("POST", "/bot/restart",     None),
    "update":   ("POST", "/bot/update",      None),
    "ping":     ("GET",  "/ping",            None),
}


def main():
    if len(sys.argv) < 2:
        print(f"{B}Tradovate Bot Remote Control{X}")
        print()
        print("Usage: python remote_ctl.py <command> [args]")
        print()
        print("Commands:")
        print(f"  {G}status{X}     Full bot status (balance, P&L, positions)")
        print(f"  {G}logs{X}       Last 50 log lines (add number for more: logs 100)")
        print(f"  {G}activity{X}   Recent signals, trades, locks")
        print(f"  {G}journal{X}    Trade journal summary")
        print(f"  {G}trades{X}     All trades list")
        print(f"  {G}token{X}      Auth token status")
        print(f"  {Y}start{X}      Start the bot")
        print(f"  {Y}stop{X}       Stop the bot")
        print(f"  {Y}restart{X}    Restart the bot")
        print(f"  {Y}update{X}     Git pull + restart")
        print(f"  {C}ping{X}       Health check")
        print()
        print(f"Config: VPS_URL={VPS_URL or '(not set)'}")
        print(f"        MGMT_API_KEY={'set' if API_KEY else '(not set)'}")
        return

    cmd = sys.argv[1].lower()
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    method, path, display_fn = COMMANDS[cmd]

    # Handle extra args
    if cmd == "logs" and len(sys.argv) > 2:
        path = f"/logs?lines={sys.argv[2]}"

    data = api_call(method, path)

    if display_fn:
        display_fn(data)
    elif cmd in ("start", "stop", "restart", "update"):
        show_action_result(data, cmd)
    else:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
