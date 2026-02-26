#!/usr/bin/env python3
"""
Server Agent — GitHub-Based Remote Control
=============================================
Runs on the VPS alongside the bot. Polls GitHub for commands,
pushes status updates back, and handles auto-deploy of code changes.

This is the bridge between Claude Code (which can only access GitHub)
and the actual server where the bot runs.

Usage:
    python server_agent.py              # Start agent (default: poll every 30s)
    python server_agent.py --interval 15  # Custom poll interval

Architecture:
    Claude Code → pushes command.json to GitHub
    Server Agent → pulls command.json, executes, pushes status.json back
    Claude Code → reads status.json from GitHub
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, date

# ─────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROL_DIR = os.path.join(BOT_DIR, "github_control")
COMMAND_FILE = os.path.join(CONTROL_DIR, "command.json")
STATUS_FILE = os.path.join(CONTROL_DIR, "status.json")
COMMAND_LOG_FILE = os.path.join(CONTROL_DIR, "command_log.json")
LOG_FILE = os.path.join(BOT_DIR, "bot.log")
TOKEN_FILE = os.path.join(BOT_DIR, ".tradovate_token.json")
JOURNAL_FILE = os.path.join(BOT_DIR, "trade_journal.json")
PID_FILE = os.path.join(BOT_DIR, ".bot.pid")
AGENT_PID_FILE = os.path.join(BOT_DIR, ".agent.pid")

BRANCH = "claude/check-bot-status-RRBLn"
POLL_INTERVAL = 30  # seconds

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] agent: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(BOT_DIR, "agent.log")),
    ],
)
logger = logging.getLogger("agent")


# ─────────────────────────────────────────
# Git Operations
# ─────────────────────────────────────────

def git_pull():
    """Pull latest changes from GitHub."""
    try:
        result = subprocess.run(
            ["git", "pull", "origin", BRANCH, "--ff-only"],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            if "Already up to date" not in output:
                logger.info("Git pull: new changes pulled")
                return "updated"
            return "no_changes"
        else:
            logger.warning("Git pull failed: %s", result.stderr.strip())
            return "error"
    except Exception as e:
        logger.error("Git pull exception: %s", e)
        return "error"


def git_push_status():
    """Commit and push status files to GitHub."""
    try:
        # Stage only control files
        subprocess.run(
            ["git", "add",
             "github_control/status.json",
             "github_control/command.json",
             "github_control/command_log.json"],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=10,
        )

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=BOT_DIR, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            return  # Nothing to commit

        # Commit
        subprocess.run(
            ["git", "commit", "-m", "agent: status update"],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=10,
        )

        # Push with retry
        for attempt in range(3):
            result = subprocess.run(
                ["git", "push", "-u", "origin", BRANCH],
                cwd=BOT_DIR, capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                logger.debug("Status pushed to GitHub")
                return
            time.sleep(2 ** attempt)

        logger.warning("Failed to push status after 3 attempts")

    except Exception as e:
        logger.error("Git push exception: %s", e)


def git_push_journal():
    """Push the trade journal to GitHub for Claude to read."""
    if not os.path.exists(JOURNAL_FILE):
        return
    try:
        subprocess.run(
            ["git", "add", "trade_journal.json"],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=10,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=BOT_DIR, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            return  # No changes

        subprocess.run(
            ["git", "commit", "-m", "agent: journal update"],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", BRANCH],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.error("Journal push error: %s", e)


# ─────────────────────────────────────────
# Bot Process Management
# ─────────────────────────────────────────

def is_bot_running():
    """Check if bot.py is running. Returns (running, pid)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*bot\\.py"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
            # Filter out our own PID and agent PID
            my_pid = os.getpid()
            pids = [p for p in pids if p != my_pid]
            if pids:
                return True, pids[0]
    except Exception:
        pass
    return False, 0


def start_bot():
    """Start the bot as a background process."""
    running, pid = is_bot_running()
    if running:
        return f"Bot already running (PID {pid})"

    try:
        log_fd = open(LOG_FILE, "a")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(BOT_DIR, "bot.py")],
            cwd=BOT_DIR,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
        # Save PID
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))

        logger.info("Bot started with PID %d", proc.pid)
        return f"Bot started (PID {proc.pid})"
    except Exception as e:
        logger.error("Failed to start bot: %s", e)
        return f"Failed to start bot: {e}"


def stop_bot():
    """Stop the bot gracefully."""
    running, pid = is_bot_running()
    if not running:
        return "Bot is not running"

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait for graceful shutdown
        for _ in range(10):
            time.sleep(1)
            still_running, _ = is_bot_running()
            if not still_running:
                logger.info("Bot stopped gracefully (was PID %d)", pid)
                return f"Bot stopped (was PID {pid})"

        # Force kill if still running
        os.kill(pid, signal.SIGKILL)
        logger.warning("Bot force-killed (PID %d)", pid)
        return f"Bot force-killed (PID {pid})"
    except ProcessLookupError:
        return "Bot process already gone"
    except Exception as e:
        return f"Error stopping bot: {e}"


def restart_bot():
    """Restart the bot."""
    stop_result = stop_bot()
    time.sleep(2)
    start_result = start_bot()
    return f"{stop_result} → {start_result}"


# ─────────────────────────────────────────
# Tradovate API Operations (via localhost)
# ─────────────────────────────────────────

def api_call(endpoint):
    """Call the bot's local remote control API."""
    try:
        import requests
        resp = requests.get(f"http://localhost:8080/api/{endpoint}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def api_command(cmd, args=None):
    """Send a command to the bot's local API."""
    try:
        import requests
        payload = {"command": cmd}
        if args:
            payload.update(args)
        resp = requests.post(
            "http://localhost:8080/api/command",
            json=payload, timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def close_all_positions():
    """Close all positions via the local API."""
    result = api_command("close-all")
    return result if result else "API not available — bot may not be running"


def cancel_all_orders():
    """Cancel all working orders via the local API."""
    result = api_command("cancel-all")
    return result if result else "API not available — bot may not be running"


def emergency_stop():
    """Emergency: close all positions, cancel orders, stop bot."""
    results = []
    results.append(f"close_positions: {close_all_positions()}")
    results.append(f"cancel_orders: {cancel_all_orders()}")
    time.sleep(2)
    results.append(f"stop_bot: {stop_bot()}")
    return " | ".join(str(r) for r in results)


def refresh_token():
    """Refresh the authentication token."""
    result = api_command("refresh-token")
    return result if result else "API not available"


# ─────────────────────────────────────────
# Status Collection
# ─────────────────────────────────────────

def collect_status():
    """Collect comprehensive status from all sources."""
    running, pid = is_bot_running()

    # Try local API first (most accurate when bot is running)
    api_status = api_call("summary") if running else None

    # Token info
    token_info = _get_token_info()

    # Journal info
    journal_info = _get_journal_info()

    # Recent log
    recent_log = _get_recent_log(20)

    # Build status object
    status = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot_running": running,
        "bot_pid": pid,
        "agent_running": True,
        "account": {},
        "risk": {},
        "positions": [],
        "recent_trades": journal_info.get("recent_trades", []),
        "journal_summary": journal_info.get("summary", {}),
        "recent_log": recent_log,
        "token": token_info,
        "errors": [],
        "uptime": _get_bot_uptime(pid) if running else "not running",
    }

    if api_status:
        # Use live API data
        balance_data = api_status.get("api_balance") or {}
        last_status = api_status.get("last_status") or {}

        status["account"] = {
            "balance": balance_data.get("totalCashValue", last_status.get("balance", 0)),
            "equity": balance_data.get("netLiq", last_status.get("balance", 0)),
            "day_pnl": last_status.get("day_pnl", 0),
            "unrealized_pnl": balance_data.get("openPnL", 0),
        }
        status["risk"] = {
            "open_contracts": last_status.get("open_contracts", 0),
            "max_contracts": last_status.get("max_contracts", 10),
            "trades_today": last_status.get("trades_today", 0) or api_status.get("today_trades", 0),
            "max_daily_trades": last_status.get("max_trades", 12),
            "locked": last_status.get("locked", False),
            "distance_to_floor": last_status.get("to_floor", 0),
        }
    else:
        # Parse from log file
        parsed = _parse_last_status_from_log()
        status["account"] = {
            "balance": parsed.get("balance", 0),
            "equity": parsed.get("balance", 0),
            "day_pnl": parsed.get("day_pnl", 0),
            "unrealized_pnl": 0,
        }
        status["risk"] = {
            "open_contracts": parsed.get("open_contracts", 0),
            "max_contracts": parsed.get("max_contracts", 10),
            "trades_today": parsed.get("trades_today", 0),
            "max_daily_trades": parsed.get("max_trades", 12),
            "locked": parsed.get("locked", False),
            "distance_to_floor": parsed.get("to_floor", 0),
        }

    return status


def _get_token_info():
    """Get token status."""
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


def _get_journal_info():
    """Get trade journal summary."""
    if not os.path.exists(JOURNAL_FILE):
        return {"summary": {}, "recent_trades": []}
    try:
        with open(JOURNAL_FILE) as f:
            data = json.load(f)
        trades = data if isinstance(data, list) else data.get("trades", [])

        today = date.today().isoformat()
        today_trades = [t for t in trades if t.get("date") == today]
        closed = [t for t in trades if t.get("status") == "closed"]
        open_trades = [t for t in trades if t.get("status") == "open"]

        total_pnl = sum(t.get("pnl", 0) for t in closed)
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) < 0]

        return {
            "summary": {
                "total_trades": len(trades),
                "today_trades": len(today_trades),
                "open_trades": len(open_trades),
                "closed_trades": len(closed),
                "total_pnl": round(total_pnl, 2),
                "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
                "wins": len(wins),
                "losses": len(losses),
            },
            "recent_trades": trades[-10:],
        }
    except Exception:
        return {"summary": {}, "recent_trades": []}


def _get_recent_log(lines=20):
    """Get recent log lines."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        result = subprocess.run(
            ["tail", f"-{lines}", LOG_FILE],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")
    except Exception:
        pass
    return []


def _parse_last_status_from_log():
    """Parse the most recent Status line from bot.log."""
    import re
    try:
        result = subprocess.run(
            ["tail", "-100", LOG_FILE],
            capture_output=True, text=True, timeout=5, cwd=BOT_DIR,
        )
        if result.returncode != 0:
            return {}

        pattern = re.compile(
            r"Status \| balance=([\d.]+) \| day_pnl=([-\d.]+) \| to_floor=([-\d.]+) \| "
            r"contracts=(\d+)/(\d+) \| trades=(\d+)/(\d+) \| locked=(\w+)"
        )
        for line in reversed(result.stdout.strip().split("\n")):
            m = pattern.search(line)
            if m:
                return {
                    "balance": float(m.group(1)),
                    "day_pnl": float(m.group(2)),
                    "to_floor": float(m.group(3)),
                    "open_contracts": int(m.group(4)),
                    "max_contracts": int(m.group(5)),
                    "trades_today": int(m.group(6)),
                    "max_trades": int(m.group(7)),
                    "locked": m.group(8) == "True",
                }
    except Exception:
        pass
    return {}


def _get_bot_uptime(pid):
    """Get bot process uptime."""
    try:
        result = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


# ─────────────────────────────────────────
# Command Processing
# ─────────────────────────────────────────

COMMAND_HANDLERS = {
    "start": lambda args: start_bot(),
    "stop": lambda args: stop_bot(),
    "restart": lambda args: restart_bot(),
    "status": lambda args: "Status update requested",
    "close_all": lambda args: close_all_positions(),
    "cancel_all": lambda args: cancel_all_orders(),
    "emergency_stop": lambda args: emergency_stop(),
    "refresh_token": lambda args: refresh_token(),
    "deploy": lambda args: deploy_and_restart(),
    "update_config": lambda args: update_config(args),
    "none": lambda args: None,  # No-op
}


def deploy_and_restart():
    """Pull latest code and restart bot."""
    pull_result = git_pull()
    if pull_result == "updated":
        restart_result = restart_bot()
        return f"Code updated, {restart_result}"
    elif pull_result == "no_changes":
        return "No code changes to deploy"
    else:
        return f"Deploy failed: git pull returned {pull_result}"


def update_config(args):
    """Update .env config values."""
    if not args:
        return "No config values provided"

    env_file = os.path.join(BOT_DIR, ".env")
    if not os.path.exists(env_file):
        return ".env file not found"

    try:
        with open(env_file) as f:
            lines = f.readlines()

        updated = []
        for key, value in args.items():
            if key in ("command", "timestamp", "id", "source"):
                continue
            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}="):
                    lines[i] = f"{key}={value}\n"
                    found = True
                    updated.append(key)
                    break
            if not found:
                lines.append(f"{key}={value}\n")
                updated.append(key)

        with open(env_file, "w") as f:
            f.writelines(lines)

        return f"Updated config: {', '.join(updated)}"
    except Exception as e:
        return f"Config update failed: {e}"


def process_command(cmd_data):
    """Process a command from command.json."""
    cmd = cmd_data.get("command", "none").lower().strip()
    cmd_id = cmd_data.get("id", "")
    args = cmd_data.get("args", {})

    if cmd == "none":
        return None

    handler = COMMAND_HANDLERS.get(cmd)
    if not handler:
        result = f"Unknown command: {cmd}"
        logger.warning(result)
    else:
        logger.info("Executing command: %s (id=%s)", cmd, cmd_id)
        try:
            result = handler(args)
        except Exception as e:
            result = f"Command error: {e}"
            logger.error("Command '%s' failed: %s", cmd, e)

    # Log the command execution
    _log_command(cmd, cmd_id, result)

    # Mark command as processed
    cmd_data["command"] = "none"
    cmd_data["last_result"] = str(result)
    cmd_data["processed_at"] = datetime.now(timezone.utc).isoformat()
    with open(COMMAND_FILE, "w") as f:
        json.dump(cmd_data, f, indent=2)

    return result


def _log_command(cmd, cmd_id, result):
    """Append to command log."""
    try:
        log = []
        if os.path.exists(COMMAND_LOG_FILE):
            with open(COMMAND_LOG_FILE) as f:
                log = json.load(f)

        log.append({
            "command": cmd,
            "id": cmd_id,
            "result": str(result),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Keep last 50 entries
        log = log[-50:]

        with open(COMMAND_LOG_FILE, "w") as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────
# Main Agent Loop
# ─────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Server Agent — GitHub Remote Control")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL, help="Poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run once and exit (for testing)")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Server Agent starting | poll_interval=%ds", args.interval)
    logger.info("Bot dir: %s", BOT_DIR)
    logger.info("Branch: %s", BRANCH)
    logger.info("=" * 60)

    # Write PID file
    with open(AGENT_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    running = True

    def handle_signal(signum, frame):
        nonlocal running
        logger.info("Signal %d received, shutting down agent...", signum)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    last_status_push = 0
    last_journal_push = 0
    last_command_id = ""
    cycle = 0

    while running:
        try:
            cycle += 1

            # 1. Pull from GitHub (check for new commands & code)
            pull_result = git_pull()

            # 2. Check for code updates → auto-restart bot
            if pull_result == "updated":
                # Check if bot.py or strategy files changed
                logger.info("Code updated from GitHub — checking if bot restart needed")
                # Simple approach: always restart on code change
                bot_running, _ = is_bot_running()
                if bot_running:
                    logger.info("Restarting bot after code update...")
                    restart_bot()

            # 3. Read and process commands
            if os.path.exists(COMMAND_FILE):
                with open(COMMAND_FILE) as f:
                    cmd_data = json.load(f)

                cmd = cmd_data.get("command", "none")
                cmd_id = cmd_data.get("id", "")

                if cmd != "none" and cmd_id != last_command_id:
                    result = process_command(cmd_data)
                    if result is not None:
                        logger.info("Command '%s' result: %s", cmd, result)
                    last_command_id = cmd_id

            # 4. Collect and write status
            status = collect_status()
            with open(STATUS_FILE, "w") as f:
                json.dump(status, f, indent=2, default=str)

            # 5. Push status to GitHub (every 2 cycles = ~60s)
            now = time.time()
            if now - last_status_push >= args.interval * 2:
                git_push_status()
                last_status_push = now

            # 6. Push journal to GitHub (every 5 minutes)
            if now - last_journal_push >= 300:
                git_push_journal()
                last_journal_push = now

            if args.once:
                break

            time.sleep(args.interval)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("Agent loop error: %s", e, exc_info=True)
            time.sleep(10)

    # Cleanup
    try:
        os.remove(AGENT_PID_FILE)
    except Exception:
        pass

    logger.info("Server Agent stopped.")


if __name__ == "__main__":
    main()
