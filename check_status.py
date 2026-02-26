#!/usr/bin/env python3
"""
Bot Health Check — Quick status report from logs and journal.

Usage:
    python check_status.py          # Full status report
    python check_status.py --watch  # Live monitoring (refreshes every 30s)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BOT_DIR = Path(__file__).parent
LOG_FILE = BOT_DIR / "bot.log"
JOURNAL_FILE = BOT_DIR / "trade_journal.json"
PID_FILE = BOT_DIR / "bot.pid"
TOKEN_FILE = BOT_DIR / ".tradovate_token.json"


def is_bot_running():
    """Check if bot process is alive."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # signal 0 = check if alive
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        return False, None


def get_last_log_lines(n=50):
    """Read last N lines from bot.log."""
    if not LOG_FILE.exists():
        return []
    try:
        lines = LOG_FILE.read_text().strip().split("\n")
        return lines[-n:]
    except OSError:
        return []


def parse_log_status(lines):
    """Extract key status info from log lines."""
    info = {
        "env": None,
        "auth_ok": False,
        "auth_method": None,
        "auth_error": None,
        "in_main_loop": False,
        "last_signal": None,
        "last_fill": None,
        "last_error": None,
        "last_activity": None,
        "token_renewed": False,
    }

    for line in lines:
        # Environment
        if "env=" in line:
            if "env=demo" in line:
                info["env"] = "demo"
            elif "env=live" in line:
                info["env"] = "live"

        # Auth success
        if "Authenticated as" in line or "token renewed" in line.lower():
            info["auth_ok"] = True
            info["token_renewed"] = "renewed" in line.lower()
        if "Web auth succeeded" in line:
            info["auth_method"] = "web"
        if "Browser auth" in line and "succeeded" in line:
            info["auth_method"] = "browser"
        if "Saved token renewed" in line:
            info["auth_method"] = "saved_token"

        # Auth failures
        if "Authentication failed" in line:
            info["auth_ok"] = False
            info["auth_error"] = "All auth methods failed"
        if "ProxyError" in line:
            info["auth_error"] = "Proxy blocking API access"
        if "Playwright" in line and "failed" in line:
            info["auth_error"] = "Playwright browser not available"

        # Main loop
        if "Entering main loop" in line:
            info["in_main_loop"] = True

        # Signals
        if "ORB" in line and "breakout" in line:
            info["last_signal"] = line.strip()
        if "VWAP" in line and ("long" in line or "short" in line):
            info["last_signal"] = line.strip()

        # Fills
        if "Fill price for orderId" in line:
            info["last_fill"] = line.strip()

        # Errors
        if "[ERROR]" in line:
            info["last_error"] = line.strip()

        # Last activity timestamp
        if line and line[0] == "2":
            try:
                info["last_activity"] = line[:23]
            except IndexError:
                pass

    return info


def get_journal_summary():
    """Get trade journal statistics."""
    if not JOURNAL_FILE.exists():
        return None
    try:
        data = json.loads(JOURNAL_FILE.read_text())
        trades = data.get("trades", [])
        open_trades = [t for t in trades if t.get("status") == "open"]
        closed = [t for t in trades if t.get("status") == "closed"]
        real_trades = [t for t in closed if t.get("entry_price", 0) > 0]

        total_pnl = sum(t.get("pnl", 0) for t in real_trades)
        wins = [t for t in real_trades if t.get("pnl", 0) > 0]
        losses = [t for t in real_trades if t.get("pnl", 0) < 0]

        return {
            "total_trades": len(trades),
            "open_trades": len(open_trades),
            "closed_trades": len(closed),
            "real_trades": len(real_trades),
            "ghost_trades": len(closed) - len(real_trades),
            "total_pnl": total_pnl,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(real_trades) * 100) if real_trades else 0,
            "open_symbols": [t["symbol"] for t in open_trades],
        }
    except (json.JSONDecodeError, OSError):
        return None


def get_token_status():
    """Check token expiration."""
    if not TOKEN_FILE.exists():
        return "No saved token"
    try:
        data = json.loads(TOKEN_FILE.read_text())
        exp = data.get("expirationTime")
        if not exp:
            return "Token exists (no expiry info)"
        exp_dt = datetime.fromisoformat(exp)
        now = datetime.now(timezone.utc)
        diff = (exp_dt - now).total_seconds()
        if diff < 0:
            return f"EXPIRED ({abs(diff)/60:.0f} min ago)"
        return f"Valid ({diff/60:.0f} min remaining)"
    except (json.JSONDecodeError, OSError):
        return "Error reading token"


def print_report():
    """Print full status report."""
    print("=" * 55)
    print("  TRADOVATE BOT — STATUS REPORT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    # Process status
    running, pid = is_bot_running()
    status_icon = "RUNNING" if running else "STOPPED"
    print(f"\n  Process:  {status_icon}" + (f" (PID {pid})" if pid else ""))

    # Token
    token_status = get_token_status()
    print(f"  Token:    {token_status}")

    # Log analysis
    lines = get_last_log_lines(100)
    if lines:
        log = parse_log_status(lines)

        env_display = log["env"] or "unknown"
        env_warn = " !! WRONG — should be demo" if log["env"] == "live" else ""
        print(f"  Env:      {env_display}{env_warn}")

        auth_display = "OK" if log["auth_ok"] else "FAILED"
        if log["auth_method"]:
            auth_display += f" (via {log['auth_method']})"
        print(f"  Auth:     {auth_display}")

        loop_display = "YES" if log["in_main_loop"] else "NO"
        print(f"  Trading:  {loop_display}")

        if log["last_activity"]:
            print(f"  Last log: {log['last_activity']}")

        if log["auth_error"]:
            print(f"\n  !! Auth issue: {log['auth_error']}")

        if log["last_error"]:
            print(f"\n  Last error:")
            print(f"    {log['last_error'][:120]}")

        if log["last_signal"]:
            print(f"\n  Last signal:")
            print(f"    {log['last_signal'][:120]}")

        if log["last_fill"]:
            print(f"\n  Last fill:")
            print(f"    {log['last_fill'][:120]}")
    else:
        print("\n  No log file found")

    # Journal
    journal = get_journal_summary()
    if journal:
        print(f"\n  {'─' * 40}")
        print(f"  JOURNAL")
        print(f"  Total trades:  {journal['total_trades']}")
        if journal["ghost_trades"] > 0:
            print(f"  Ghost trades:  {journal['ghost_trades']} (entry_price=0)")
        print(f"  Real trades:   {journal['real_trades']}")
        print(f"  Open now:      {journal['open_trades']}", end="")
        if journal["open_symbols"]:
            print(f" ({', '.join(journal['open_symbols'])})", end="")
        print()

        if journal["real_trades"] > 0:
            print(f"  Wins/Losses:   {journal['wins']}/{journal['losses']}")
            print(f"  Win rate:      {journal['win_rate']:.0f}%")
            print(f"  Total P&L:     ${journal['total_pnl']:,.2f}")

    print(f"\n{'=' * 55}")

    # Health verdict
    if running and log.get("auth_ok") and log.get("in_main_loop"):
        print("  STATUS: ALL GOOD — Bot is live and trading")
    elif running and log.get("auth_ok"):
        print("  STATUS: STARTING — Authenticated, waiting for main loop")
    elif running:
        print("  STATUS: WARNING — Running but auth may have issues")
    else:
        print("  STATUS: DOWN — Bot is not running")
    print("=" * 55)


def watch_mode():
    """Continuous monitoring mode."""
    try:
        while True:
            os.system("clear" if os.name != "nt" else "cls")
            print_report()
            print(f"\n  Refreshing every 30s... (Ctrl+C to stop)")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch_mode()
    else:
        print_report()
