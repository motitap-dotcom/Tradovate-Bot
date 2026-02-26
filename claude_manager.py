#!/usr/bin/env python3
"""
Claude Manager — Remote Bot Control via GitHub
=================================================
Helper script that Claude Code uses to manage the bot remotely.
All communication happens through GitHub (git push/pull).

Usage (from Claude Code):
    python claude_manager.py status          # Read current bot status
    python claude_manager.py send start      # Start the bot
    python claude_manager.py send stop       # Stop the bot
    python claude_manager.py send restart    # Restart the bot
    python claude_manager.py send close_all  # Close all positions
    python claude_manager.py send emergency_stop  # Emergency: close all + stop
    python claude_manager.py journal         # Show trade journal summary
    python claude_manager.py log             # Show recent bot logs
    python claude_manager.py history         # Show command history

Available Commands:
    start           — Start the bot
    stop            — Stop the bot
    restart         — Restart the bot
    close_all       — Close all open positions
    cancel_all      — Cancel all working orders
    emergency_stop  — Close positions + cancel orders + stop bot
    refresh_token   — Refresh authentication token
    deploy          — Pull latest code and restart bot
    update_config   — Update .env values (pass key=value in args)
    status          — Force a status update
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROL_DIR = os.path.join(BOT_DIR, "github_control")
COMMAND_FILE = os.path.join(CONTROL_DIR, "command.json")
STATUS_FILE = os.path.join(CONTROL_DIR, "status.json")
COMMAND_LOG_FILE = os.path.join(CONTROL_DIR, "command_log.json")
BRANCH = "claude/check-bot-status-RRBLn"


def git_pull():
    """Pull latest from GitHub."""
    result = subprocess.run(
        ["git", "pull", "origin", BRANCH, "--ff-only"],
        cwd=BOT_DIR, capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


def git_push():
    """Push changes to GitHub."""
    subprocess.run(
        ["git", "add", "github_control/"],
        cwd=BOT_DIR, capture_output=True, text=True, timeout=10,
    )
    subprocess.run(
        ["git", "commit", "-m", "claude: send command"],
        cwd=BOT_DIR, capture_output=True, text=True, timeout=10,
    )
    for attempt in range(3):
        result = subprocess.run(
            ["git", "push", "-u", "origin", BRANCH],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True
        import time
        time.sleep(2 ** attempt)
    return False


def send_command(cmd, args=None):
    """Write a command to command.json and push to GitHub."""
    # Pull first to avoid conflicts
    git_pull()

    command_data = {
        "command": cmd,
        "args": args or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "id": str(uuid.uuid4())[:8],
        "source": "claude",
    }

    with open(COMMAND_FILE, "w") as f:
        json.dump(command_data, f, indent=2)

    if git_push():
        print(f"Command '{cmd}' sent successfully (id={command_data['id']})")
        print("The server agent will execute it within ~30 seconds.")
        return True
    else:
        print(f"ERROR: Failed to push command to GitHub")
        return False


def read_status():
    """Pull and read the latest status."""
    git_pull()

    if not os.path.exists(STATUS_FILE):
        print("No status file found. Is the server agent running?")
        return None

    with open(STATUS_FILE) as f:
        status = json.load(f)

    ts = status.get("timestamp", "unknown")
    bot_running = status.get("bot_running", False)
    agent_running = status.get("agent_running", False)
    account = status.get("account", {})
    risk = status.get("risk", {})
    token = status.get("token", {})
    journal = status.get("journal_summary", {})

    print("=" * 60)
    print(f"  BOT STATUS — {ts}")
    print("=" * 60)
    print(f"  Agent: {'RUNNING' if agent_running else 'STOPPED'}")
    print(f"  Bot:   {'RUNNING' if bot_running else 'STOPPED'} (PID: {status.get('bot_pid', 'N/A')})")
    print(f"  Uptime: {status.get('uptime', 'N/A')}")
    print()
    print("  ACCOUNT:")
    print(f"    Balance:      ${account.get('balance', 0):,.2f}")
    print(f"    Equity:       ${account.get('equity', 0):,.2f}")
    print(f"    Day P&L:      ${account.get('day_pnl', 0):,.2f}")
    print(f"    Unrealized:   ${account.get('unrealized_pnl', 0):,.2f}")
    print()
    print("  RISK:")
    print(f"    Contracts:    {risk.get('open_contracts', 0)}/{risk.get('max_contracts', 10)}")
    print(f"    Trades today: {risk.get('trades_today', 0)}/{risk.get('max_daily_trades', 12)}")
    print(f"    To floor:     ${risk.get('distance_to_floor', 0):,.2f}")
    print(f"    Locked:       {risk.get('locked', False)}")
    print()
    print("  TOKEN:")
    print(f"    Status:       {token.get('status', 'unknown')}")
    print(f"    Remaining:    {token.get('minutes_remaining', 0)} minutes")
    print()
    if journal:
        print("  JOURNAL:")
        print(f"    Total trades:  {journal.get('total_trades', 0)}")
        print(f"    Today trades:  {journal.get('today_trades', 0)}")
        print(f"    Win rate:      {journal.get('win_rate', 0)}%")
        print(f"    Total P&L:     ${journal.get('total_pnl', 0):,.2f}")
    print()

    # Recent log
    logs = status.get("recent_log", [])
    if logs:
        print("  RECENT LOG:")
        for line in logs[-5:]:
            print(f"    {line}")
    print("=" * 60)

    return status


def read_journal():
    """Pull and show trade journal."""
    git_pull()

    journal_file = os.path.join(BOT_DIR, "trade_journal.json")
    if not os.path.exists(journal_file):
        print("No trade journal found.")
        return

    with open(journal_file) as f:
        data = json.load(f)

    trades = data if isinstance(data, list) else data.get("trades", [])
    closed = [t for t in trades if t.get("status") == "closed"]
    open_trades = [t for t in trades if t.get("status") == "open"]

    print("=" * 60)
    print("  TRADE JOURNAL")
    print("=" * 60)
    print(f"  Total: {len(trades)} | Closed: {len(closed)} | Open: {len(open_trades)}")

    if closed:
        total_pnl = sum(t.get("pnl", 0) for t in closed)
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) < 0]
        print(f"  P&L: ${total_pnl:,.2f} | Wins: {len(wins)} | Losses: {len(losses)}")
        if closed:
            print(f"  Win rate: {len(wins)/len(closed)*100:.1f}%")
    print()

    # Show last 10 trades
    print("  RECENT TRADES:")
    for t in trades[-10:]:
        sym = t.get("symbol", "?")
        direction = t.get("direction", "?")
        pnl = t.get("pnl", 0)
        status = t.get("status", "?")
        entry = t.get("entry_price", 0)
        reason = t.get("exit_reason", t.get("reason", ""))
        dt = t.get("date", "")
        print(f"    {dt} | {sym:3s} {direction:4s} @ {entry:>10.2f} | "
              f"P&L: ${pnl:>8.2f} | {status:6s} | {reason}")
    print("=" * 60)


def read_log():
    """Show recent log from status file."""
    git_pull()

    if not os.path.exists(STATUS_FILE):
        print("No status file found.")
        return

    with open(STATUS_FILE) as f:
        status = json.load(f)

    logs = status.get("recent_log", [])
    if logs:
        for line in logs:
            print(line)
    else:
        print("No recent log lines available.")


def read_history():
    """Show command execution history."""
    git_pull()

    if not os.path.exists(COMMAND_LOG_FILE):
        print("No command history found.")
        return

    with open(COMMAND_LOG_FILE) as f:
        log = json.load(f)

    print("=" * 60)
    print("  COMMAND HISTORY")
    print("=" * 60)
    for entry in log[-20:]:
        ts = entry.get("timestamp", "?")[:19]
        cmd = entry.get("command", "?")
        result = entry.get("result", "?")
        print(f"  {ts} | {cmd:20s} | {result}")
    print("=" * 60)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1].lower()

    if action == "status":
        read_status()

    elif action == "send":
        if len(sys.argv) < 3:
            print("Usage: python claude_manager.py send <command> [key=value ...]")
            print("Commands: start, stop, restart, close_all, cancel_all, "
                  "emergency_stop, refresh_token, deploy, update_config, status")
            return

        cmd = sys.argv[2].lower()
        args = {}
        for arg in sys.argv[3:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                args[k] = v

        send_command(cmd, args if args else None)

    elif action == "journal":
        read_journal()

    elif action == "log":
        read_log()

    elif action == "history":
        read_history()

    else:
        print(f"Unknown action: {action}")
        print("Actions: status, send, journal, log, history")


if __name__ == "__main__":
    main()
