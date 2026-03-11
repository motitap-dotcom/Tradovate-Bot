#!/usr/bin/env python3
"""
Bot Status Dashboard (Terminal)
================================
Rich terminal dashboard for the trading bot.

Usage:
    python status.py          # One-time snapshot
    python status.py --watch  # Live refresh every 10s
    python status.py --full   # Full report with journal + prices
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
TUNER_LOG = os.path.join(BOT_DIR, "tuner_log.json")

# ANSI colors
G = "\033[32m"   # green
R = "\033[31m"   # red
Y = "\033[33m"   # yellow
B = "\033[34m"   # blue
C = "\033[36m"   # cyan
DIM = "\033[2m"  # dim
BOLD = "\033[1m"
X = "\033[0m"    # reset
BG_G = "\033[42m\033[30m"  # green background
BG_R = "\033[41m\033[37m"  # red background

W = 58  # dashboard width


def _bar(pct, width=30):
    """Render a progress bar."""
    filled = int(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    color = G if pct > 0 else R
    return f"{color}{bar}{X} {pct:.1f}%"


def _pnl(val):
    """Format P&L with color."""
    s = "+" if val >= 0 else ""
    c = G if val >= 0 else R
    return f"{c}{s}${val:,.2f}{X}"


def is_bot_running():
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "bot.py"], text=True, stderr=subprocess.DEVNULL
        )
        pids = [int(p) for p in out.strip().split("\n") if p]
        return bool(pids), pids[0] if pids else 0
    except subprocess.CalledProcessError:
        return False, 0


def get_last_status():
    result = {
        "balance": 0, "day_pnl": 0, "to_floor": 0,
        "contracts": "0/0", "trades": "0/0", "locked": "False",
        "timestamp": "", "age_seconds": 999,
    }
    if not os.path.exists(LOG_FILE):
        return result
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


def get_recent_activity():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", "-200", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return []
    items = []
    for line in out.strip().split("\n"):
        if any(kw in line for kw in ["SIGNAL:", "LOCKED", "bracket order", "Force close", "Journal: ENTRY", "Journal: EXIT"]):
            items.append(line.strip())
    return items[-8:]


def get_token_status():
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


def get_journal():
    if not os.path.exists(JOURNAL_FILE):
        return {}
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def get_prices():
    """Fetch current futures prices."""
    try:
        import requests
        symbols = {"NQ": "NQ=F", "ES": "ES=F", "GC": "GC=F", "CL": "CL=F"}
        prices = {}
        for name, sym in symbols.items():
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            if resp.status_code == 200:
                meta = resp.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice", 0)
                prev = meta.get("chartPreviousClose", price)
                prices[name] = {"price": price, "change": price - prev}
        return prices
    except Exception:
        return {}


def get_tuner_adjustments():
    if not os.path.exists(TUNER_LOG):
        return []
    try:
        with open(TUNER_LOG) as f:
            return json.load(f)[-5:]
    except Exception:
        return []


def display(full=False):
    """Print the dashboard."""
    running, pid = is_bot_running()
    status = get_last_status()
    token_status, token_mins = get_token_status()
    activity = get_recent_activity()
    journal_data = get_journal()
    journal = journal_data.get("summary", {})

    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if "--watch" in sys.argv or "--full" in sys.argv:
        print("\033[2J\033[H", end="")

    # Header
    print(f"{BOLD}{'=' * W}{X}")
    print(f"{BOLD}  TRADOVATE BOT DASHBOARD{X}")
    print(f"{'=' * W}")

    # Status row
    if running:
        bot_st = f"{BG_G} RUNNING {X} PID {pid}"
    else:
        bot_st = f"{BG_R} STOPPED {X}"

    if token_status == "valid":
        tok_st = f"{G}OK{X} ({token_mins:.0f}m)"
    elif token_status == "EXPIRED":
        tok_st = f"{R}EXPIRED{X}"
    else:
        tok_st = f"{Y}{token_status}{X}"

    age = status["age_seconds"]
    freshness = f"{age:.0f}s" if age < 60 else f"{age/60:.0f}m" if age < 3600 else "stale"

    print(f"  Bot: {bot_st}  |  Token: {tok_st}  |  Data: {freshness}")

    # Balance section
    print(f"{'-' * W}")
    balance = status["balance"]
    day_pnl = status["day_pnl"]
    to_floor = status["to_floor"]

    print(f"  {BOLD}Balance:    ${balance:>12,.2f}{X}")
    print(f"  Day P&L:  {_pnl(day_pnl):>22}")
    floor_color = R if to_floor < 500 else Y if to_floor < 1000 else G
    print(f"  To Floor: {floor_color}${to_floor:>12,.2f}{X}")

    # Challenge progress (with consistency rule)
    profit = balance - 50000
    base_target = journal.get("base_target", 3000)
    effective_target = journal.get("effective_target", base_target)
    consistency_adjusted = journal.get("consistency_adjusted", False)
    highest_day = journal.get("highest_day_profit", 0)

    pct = max(0, (profit / effective_target) * 100) if effective_target else 0
    remaining = max(0, effective_target - profit)
    print(f"  Progress: {_bar(min(100, pct))}")
    if consistency_adjusted:
        print(f"  {Y}Target: ${effective_target:,.0f} (raised from ${base_target:,.0f} — consistency rule){X}")
        print(f"  {DIM}Highest day: ${highest_day:,.2f} / max 40% of total{X}")
        print(f"  {DIM}Remaining:   ${remaining:,.2f}{X}")
        if profit >= base_target and profit < effective_target:
            print(f"  {Y}Profit target reached, but consistency not met yet (${remaining:,.0f} more needed){X}")
    elif profit >= base_target:
        print(f"  {G}Target reached!{X}")
    else:
        print(f"  {DIM}Target: ${base_target:,.0f} | Remaining: ${max(0, base_target - profit):,.2f}{X}")

    # Trading stats
    print(f"{'-' * W}")
    print(f"  Contracts: {status['contracts']}  |  Trades: {status['trades']}", end="")
    if status["locked"] == "True":
        print(f"  | {R}{BOLD}LOCKED{X}", end="")
    print()

    # Market prices (if --full)
    if full:
        print(f"{'-' * W}")
        print(f"  {DIM}Market Prices:{X}")
        prices = get_prices()
        if prices:
            parts = []
            for sym, d in prices.items():
                chg = d["change"]
                c = G if chg >= 0 else R
                s = "+" if chg >= 0 else ""
                parts.append(f"  {sym}: ${d['price']:>10,.2f} {c}{s}{chg:,.2f}{X}")
            # Print in 2 columns
            for i in range(0, len(parts), 2):
                row = parts[i]
                if i + 1 < len(parts):
                    row = f"{parts[i]:<35}{parts[i+1]}"
                print(row)
        else:
            print(f"  {DIM}(unavailable){X}")

    # Journal summary
    if journal and journal.get("total_trades", 0) > 0:
        print(f"{'-' * W}")
        total = journal.get("total_trades", 0)
        wins = journal.get("wins", 0)
        losses = journal.get("losses", 0)
        wr = journal.get("win_rate", 0)
        total_pnl = journal.get("total_pnl", 0)
        pf = journal.get("profit_factor", 0)
        exp = journal.get("expectancy", 0)

        wr_color = G if wr >= 0.5 else Y if wr >= 0.4 else R
        print(f"  {BOLD}Journal:{X}  {total} trades  |  {wr_color}{wr:.0%} WR{X}  ({wins}W/{losses}L)")
        print(f"  Total: {_pnl(total_pnl)}  |  PF: {pf:.2f}  |  Exp: {_pnl(exp)}/trade")

        # Per-symbol breakdown (if full)
        if full:
            trades_list = journal_data.get("trades", [])
            by_sym = {}
            for t in trades_list:
                if t.get("status") == "closed" and t.get("pnl") is not None:
                    sym = t["symbol"]
                    by_sym.setdefault(sym, []).append(t["pnl"])
            if by_sym:
                print(f"  {'Symbol':<8} {'Trades':>6} {'WR':>6} {'P&L':>12}")
                for sym in sorted(by_sym):
                    pnls = by_sym[sym]
                    w = len([p for p in pnls if p > 0])
                    total_p = sum(pnls)
                    wr_s = w / len(pnls) if pnls else 0
                    print(f"  {sym:<8} {len(pnls):>6} {wr_s:>5.0%} {_pnl(total_p):>22}")

    # Lessons (if full)
    if full:
        try:
            from trade_journal import TradeJournal
            tj = TradeJournal()
            lessons = tj.generate_lessons()
            if lessons and lessons[0] != "Not enough trades yet (need at least 3 closed trades for analysis).":
                print(f"{'-' * W}")
                print(f"  {BOLD}Lessons:{X}")
                for i, lesson in enumerate(lessons, 1):
                    # Wrap long lines
                    print(f"  {C}{i}.{X} {lesson[:70]}")
                    if len(lesson) > 70:
                        print(f"     {lesson[70:]}")
        except Exception:
            pass

    # Auto-tuner adjustments (if full)
    if full:
        adjustments = get_tuner_adjustments()
        if adjustments:
            print(f"{'-' * W}")
            print(f"  {BOLD}Auto-Tuner:{X}")
            for a in adjustments[-3:]:
                sym = a.get("symbol", "?")
                param = a.get("param", "?")
                old = a.get("old_value", "?")
                new = a.get("new_value", "?")
                print(f"  {Y}{sym}.{param}{X}: {old} -> {G}{new}{X}")
                print(f"    {DIM}{a.get('reason', '')}{X}")

    # Recent activity
    if activity:
        print(f"{'-' * W}")
        print(f"  {BOLD}Recent Activity:{X}")
        for line in activity[-5:]:
            # Color code by type
            short = line[20:] if len(line) > 20 else line
            if "SIGNAL:" in short or "ENTRY" in short:
                print(f"  {G}>{X} {short[:68]}")
            elif "LOCKED" in short:
                print(f"  {R}!{X} {short[:68]}")
            elif "EXIT" in short:
                if "WIN" in short:
                    print(f"  {G}${X} {short[:68]}")
                elif "LOSS" in short:
                    print(f"  {R}${X} {short[:68]}")
                else:
                    print(f"  {Y}${X} {short[:68]}")
            else:
                print(f"  {DIM}  {short[:68]}{X}")

    print(f"{'=' * W}")
    print(f"  {DIM}{now_utc} | Refresh: python status.py{X}")


def main():
    full = "--full" in sys.argv
    if "--watch" in sys.argv:
        try:
            while True:
                display(full=full)
                time.sleep(10)
        except KeyboardInterrupt:
            pass
    else:
        display(full=full)


if __name__ == "__main__":
    main()
