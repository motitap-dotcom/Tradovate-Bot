#!/usr/bin/env python3
"""
Bot Commander — Send commands to VPS via GitHub
=================================================
Used from Claude Code to control the bot remotely.
Writes a command to .bot_command.json, pushes to GitHub,
then waits for the VPS agent to execute and push results.

Usage:
    python bot_cmd.py status         # Get bot status
    python bot_cmd.py start          # Start the bot
    python bot_cmd.py stop           # Stop the bot
    python bot_cmd.py restart        # Restart the bot
    python bot_cmd.py logs           # View recent logs
    python bot_cmd.py activity       # Recent signals/trades
    python bot_cmd.py update         # Git pull + restart on VPS
    python bot_cmd.py token          # Token status
    python bot_cmd.py ping           # Health check
    python bot_cmd.py read           # Just read latest status (no command)
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMAND_FILE = os.path.join(BOT_DIR, ".bot_command.json")
STATUS_FILE = os.path.join(BOT_DIR, ".bot_status.json")

MAX_WAIT = 120  # Max seconds to wait for response
POLL_INTERVAL = 10  # Check every N seconds


def git(*args, timeout=30):
    cmd = ["git", "-C", BOT_DIR] + list(args)
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=timeout).strip()
    except subprocess.CalledProcessError as e:
        return f"ERROR: {e.output.strip()}"
    except subprocess.TimeoutExpired:
        return "ERROR: timeout"


def get_branch():
    return git("branch", "--show-current") or "master"


def send_command(command, args=None):
    """Write command to file and push to GitHub."""
    command_id = str(uuid.uuid4())[:8]
    cmd_data = {
        "command": command,
        "command_id": command_id,
        "args": args or {},
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(COMMAND_FILE, "w") as f:
        json.dump(cmd_data, f, indent=2)

    branch = get_branch()
    git("add", COMMAND_FILE)
    git("commit", "-m", f"Command: {command} [{command_id}]")

    # Push with retry
    for attempt in range(4):
        result = git("push", "origin", branch)
        if "error" not in result.lower() and "fatal" not in result.lower():
            return command_id
        wait = 2 ** (attempt + 1)
        time.sleep(wait)

    print("Failed to push command to GitHub.")
    return None


def wait_for_result(command_id):
    """Poll GitHub for the result."""
    branch = get_branch()
    start = time.time()

    while time.time() - start < MAX_WAIT:
        git("fetch", "origin", branch)
        git("merge", "origin/" + branch, "--ff-only")

        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE) as f:
                    status = json.load(f)
                if status.get("command_id") == command_id:
                    return status
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)

    return None


def read_latest_status():
    """Just fetch and read the latest status without sending a command."""
    branch = get_branch()
    git("fetch", "origin", branch)
    git("merge", "origin/" + branch, "--ff-only")

    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return None


# ── Display ──────────────────────────────────────────────

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


def display_status(data):
    """Pretty-print status data."""
    if not data:
        print("No data available.")
        return

    bot = data.get("bot", {})
    trading = data.get("trading", {})
    token = data.get("token", {})
    journal = data.get("journal", {})

    print(f"{B}{'=' * 55}{X}")
    print(f"{B}  TRADOVATE BOT — LIVE STATUS{X}")
    print(f"{'=' * 55}")

    # Bot
    if bot.get("running"):
        print(f"  Bot:     {G}RUNNING{X} (PID {bot.get('pid', '?')}) | systemd: {bot.get('systemd', '?')}")
    else:
        print(f"  Bot:     {R}STOPPED{X} | systemd: {bot.get('systemd', '?')}")

    # Token
    tok = token.get("status", "unknown")
    mins = token.get("remaining_minutes", 0)
    if tok == "valid":
        print(f"  Token:   {G}OK{X} ({mins:.0f} min left)")
    elif tok == "expired":
        print(f"  Token:   {R}EXPIRED{X}")
    else:
        print(f"  Token:   {Y}{tok}{X}")

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
    filled = int(pct / 100 * 30)
    bar = "█" * filled + "░" * (30 - filled)
    bar_c = G if pct > 0 else R
    print(f"  Progress:   {bar_c}{bar}{X} {pct:.1f}%")

    # Trades
    print(f"{'-' * 55}")
    contracts = trading.get("contracts", "0/0")
    trades = trading.get("trades", "0/0")
    locked = trading.get("locked", False)
    lock_str = f"  | {R}{B}LOCKED{X}" if locked else ""
    print(f"  Contracts: {contracts}  |  Trades: {trades}{lock_str}")

    # Journal
    total = journal.get("total_trades", 0)
    if total > 0:
        wr = journal.get("win_rate", 0)
        print(f"  Journal:   {total} trades | {wr:.0%} WR | P&L: {_pnl(journal.get('total_pnl', 0))}")

    print(f"{'=' * 55}")
    print(f"  {D}{data.get('server_time', '')}{X}")


def display_result(status_data):
    """Display command result."""
    if not status_data:
        print(f"{R}No response from VPS (timeout).{X}")
        print("Is vps_agent.py running on your VPS?")
        return

    result = status_data.get("result", {})
    executed = status_data.get("executed_at", "")
    cmd_id = status_data.get("command_id", "")

    if not result.get("ok", False):
        print(f"{R}Command failed:{X} {result.get('message', 'unknown error')}")
        return

    msg = result.get("message", "")
    data = result.get("data", {})

    if msg:
        print(f"{G}OK:{X} {msg}")

    if data:
        # If it's a full status
        if "bot" in data:
            display_status(data)
        # If it's logs
        elif "lines" in data:
            for line in data["lines"]:
                if "ERROR" in line or "LOCKED" in line:
                    print(f"{R}{line}{X}")
                elif "SIGNAL:" in line or "ENTRY" in line:
                    print(f"{G}{line}{X}")
                elif "WARNING" in line:
                    print(f"{Y}{line}{X}")
                else:
                    print(line)
        # If it's activity
        elif "activity" in data:
            for line in data["activity"]:
                print(f"  {line}")
        # Generic JSON
        else:
            print(json.dumps(data, indent=2))

    print(f"\n{D}Executed: {executed} | ID: {cmd_id}{X}")


# ── Main ─────────────────────────────────────────────────


COMMANDS = ["status", "start", "stop", "restart", "logs", "activity", "update", "token", "ping", "read"]


def main():
    if len(sys.argv) < 2:
        print(f"{B}Bot Commander — Control bot via GitHub{X}")
        print()
        print("Usage: python bot_cmd.py <command>")
        print()
        for cmd in COMMANDS:
            print(f"  {G}{cmd}{X}")
        return

    cmd = sys.argv[1].lower()
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)

    # Just read latest status
    if cmd == "read":
        print(f"{D}Fetching latest status from GitHub...{X}")
        status = read_latest_status()
        if status:
            display_result(status)
        else:
            print("No status file found. VPS agent may not have run yet.")
        return

    # Send command and wait
    print(f"{D}Sending '{cmd}' command to VPS via GitHub...{X}")
    args = {}
    if cmd == "logs" and len(sys.argv) > 2:
        args["lines"] = int(sys.argv[2])

    command_id = send_command(cmd, args)
    if not command_id:
        sys.exit(1)

    print(f"{D}Command pushed (id={command_id}). Waiting for VPS agent...{X}")
    result = wait_for_result(command_id)
    display_result(result)


if __name__ == "__main__":
    main()
