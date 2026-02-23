#!/usr/bin/env python3
"""
Bot Status Dashboard
====================
Quick visual status of the trading bot.

Usage:
    python status.py          # One-time snapshot
    python status.py --watch  # Live refresh every 10s
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BOT_DIR, "bot.log")
TOKEN_FILE = os.path.join(BOT_DIR, ".tradovate_token.json")
JOURNAL_FILE = os.path.join(BOT_DIR, "trade_journal.json")


def is_bot_running() -> tuple[bool, int]:
    """Check if bot.py process is alive."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "bot.py"], text=True, stderr=subprocess.DEVNULL
        )
        pids = [int(p) for p in out.strip().split("\n") if p]
        return bool(pids), pids[0] if pids else 0
    except subprocess.CalledProcessError:
        return False, 0


def get_last_status() -> dict:
    """Parse the most recent status line from bot.log."""
    result = {
        "balance": 0, "day_pnl": 0, "to_floor": 0,
        "contracts": "0/0", "trades": "0/0", "locked": "False",
        "timestamp": "", "age_seconds": 999,
    }
    if not os.path.exists(LOG_FILE):
        return result

    # Read last 50 lines efficiently
    try:
        out = subprocess.check_output(
            ["tail", "-50", LOG_FILE], text=True, stderr=subprocess.DEVNULL
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
                "locked": m.group(7),
                "timestamp": m.group(1),
                "age_seconds": age,
            }
    return result


def get_recent_signals() -> list[str]:
    """Get recent trade signals from log."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", "-200", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return []

    signals = []
    for line in out.strip().split("\n"):
        if any(kw in line for kw in ["Signal:", "LOCKED", "bracket order", "Force close"]):
            signals.append(line.strip())
    return signals[-5:]  # Last 5


def get_token_status() -> tuple[str, float]:
    """Check token expiration."""
    if not os.path.exists(TOKEN_FILE):
        return "missing", 0
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        exp = data.get("expirationTime", "")
        if not exp:
            return "unknown", 0
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
        if remaining <= 0:
            return "EXPIRED", remaining
        return "valid", remaining
    except Exception:
        return "error", 0


def get_journal_summary() -> dict:
    """Get trade journal stats."""
    if not os.path.exists(JOURNAL_FILE):
        return {}
    try:
        with open(JOURNAL_FILE) as f:
            journal = json.load(f)
        return journal.get("summary", {})
    except Exception:
        return {}


def display():
    """Print status dashboard."""
    running, pid = is_bot_running()
    status = get_last_status()
    token_status, token_mins = get_token_status()
    signals = get_recent_signals()
    journal = get_journal_summary()

    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Clear screen for --watch mode
    if "--watch" in sys.argv:
        print("\033[2J\033[H", end="")

    print("=" * 55)
    print("  TRADOVATE BOT STATUS")
    print("=" * 55)

    # Bot process
    if running:
        print(f"  Process:  RUNNING (PID {pid})")
    else:
        print(f"  Process:  STOPPED")

    # Token
    if token_status == "valid":
        print(f"  Token:    OK ({token_mins:.0f} min remaining)")
    elif token_status == "EXPIRED":
        print(f"  Token:    EXPIRED!")
    else:
        print(f"  Token:    {token_status}")

    # Data freshness
    age = status["age_seconds"]
    if age < 60:
        freshness = f"{age:.0f}s ago"
    elif age < 3600:
        freshness = f"{age/60:.0f}m ago"
    else:
        freshness = "stale"
    print(f"  Data:     {freshness} ({now_utc})")

    print("-" * 55)

    # Balance
    balance = status["balance"]
    day_pnl = status["day_pnl"]
    pnl_sign = "+" if day_pnl >= 0 else ""
    pnl_color = "\033[32m" if day_pnl >= 0 else "\033[31m"
    reset = "\033[0m"

    print(f"  Balance:  ${balance:,.2f}")
    print(f"  Day P&L:  {pnl_color}{pnl_sign}${day_pnl:,.2f}{reset}")
    print(f"  To Floor: ${status['to_floor']:,.2f}")
    print(f"  Contracts:{status['contracts']}  |  Trades: {status['trades']}")

    if status["locked"] == "True":
        print(f"  \033[31m** TRADING LOCKED **\033[0m")

    # Journal summary
    if journal:
        print("-" * 55)
        total = journal.get("total_trades", 0)
        wins = journal.get("wins", 0)
        wr = journal.get("win_rate", 0)
        total_pnl = journal.get("total_pnl", 0)
        print(f"  Journal:  {total} trades | WR: {wr:.0%} | Total: ${total_pnl:+,.2f}")

    # Recent signals
    if signals:
        print("-" * 55)
        print("  Recent Activity:")
        for s in signals:
            # Trim timestamp prefix for readability
            short = s[20:] if len(s) > 20 else s
            print(f"    {short[:70]}")

    print("=" * 55)


def main():
    if "--watch" in sys.argv:
        try:
            while True:
                display()
                time.sleep(10)
        except KeyboardInterrupt:
            pass
    else:
        display()


if __name__ == "__main__":
    main()
