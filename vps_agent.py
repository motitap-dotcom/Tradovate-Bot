#!/usr/bin/env python3
"""
VPS Agent — GitHub-Based Remote Control
==========================================
Runs on the VPS and polls GitHub for commands from Claude Code.

How it works:
    1. Claude Code pushes a command to .bot_command.json in the repo
    2. This agent pulls from GitHub every POLL_INTERVAL seconds
    3. Reads the command, executes it, writes result to .bot_status.json
    4. Pushes the result back to GitHub
    5. Claude Code reads the result via git fetch

Usage:
    python vps_agent.py              # Run agent (polls every 15s)
    python vps_agent.py --once       # Execute pending command once and exit

Commands supported (written to .bot_command.json):
    start, stop, restart, status, logs, activity, update, ping
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMAND_FILE = os.path.join(BOT_DIR, ".bot_command.json")
STATUS_FILE = os.path.join(BOT_DIR, ".bot_status.json")
LOG_FILE = os.path.join(BOT_DIR, "bot.log")
TOKEN_FILE = os.path.join(BOT_DIR, ".tradovate_token.json")
JOURNAL_FILE = os.path.join(BOT_DIR, "trade_journal.json")

SERVICE_NAME = "tradovate-bot"
POLL_INTERVAL = 15  # seconds
BRANCH = None  # auto-detect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(BOT_DIR, "agent.log")),
    ],
)
log = logging.getLogger("agent")


# ── Git helpers ──────────────────────────────────────────


def git(*args, timeout=30):
    """Run a git command and return output."""
    cmd = ["git", "-C", BOT_DIR] + list(args)
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=timeout).strip()
    except subprocess.CalledProcessError as e:
        log.warning("git %s failed: %s", " ".join(args), e.output.strip())
        return ""
    except subprocess.TimeoutExpired:
        log.warning("git %s timed out", " ".join(args))
        return ""


def get_branch():
    global BRANCH
    if not BRANCH:
        BRANCH = git("branch", "--show-current") or "master"
    return BRANCH


def pull():
    """Pull latest from GitHub."""
    branch = get_branch()
    result = git("pull", "origin", branch)
    return result


def push_status():
    """Push status file to GitHub."""
    branch = get_branch()
    git("add", STATUS_FILE)
    git("commit", "-m", "Agent: status update")
    for attempt in range(4):
        result = git("push", "origin", branch)
        if "error" not in result.lower() and "fatal" not in result.lower():
            return True
        wait = 2 ** (attempt + 1)
        log.warning("Push failed (attempt %d), retrying in %ds...", attempt + 1, wait)
        time.sleep(wait)
    return False


# ── Data collection (same as status.py / remote_api.py) ─


def is_bot_running():
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "python.*bot\\.py"], text=True, stderr=subprocess.DEVNULL
        )
        lines = [l for l in out.strip().split("\n") if l and "vps_agent" not in l]
        if lines:
            return True, int(lines[0].split()[0])
        return False, 0
    except subprocess.CalledProcessError:
        return False, 0


def get_systemd_status():
    try:
        return subprocess.check_output(
            ["systemctl", "is-active", SERVICE_NAME], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip() or "unknown"


def get_last_status():
    result = {
        "balance": 0, "day_pnl": 0, "to_floor": 0,
        "contracts": "0/0", "trades": "0/0", "locked": False,
        "timestamp": "", "age_seconds": -1,
    }
    if not os.path.exists(LOG_FILE):
        return result
    try:
        out = subprocess.check_output(
            ["tail", "-100", LOG_FILE], text=True, stderr=subprocess.DEVNULL
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
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            return {
                "balance": float(m.group(2)),
                "day_pnl": float(m.group(3)),
                "to_floor": float(m.group(4)),
                "contracts": m.group(5),
                "trades": m.group(6),
                "locked": m.group(7) == "True",
                "timestamp": m.group(1),
                "age_seconds": round(age, 1),
            }
    return result


def get_token_status():
    if not os.path.exists(TOKEN_FILE):
        return {"status": "missing", "remaining_minutes": 0}
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        exp = data.get("expirationTime", "")
        if not exp:
            return {"status": "unknown", "remaining_minutes": 0}
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 60
        if remaining <= 0:
            return {"status": "expired", "remaining_minutes": round(remaining, 1)}
        return {"status": "valid", "remaining_minutes": round(remaining, 1)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def get_journal_summary():
    if not os.path.exists(JOURNAL_FILE):
        return {"total_trades": 0}
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f).get("summary", {"total_trades": 0})
    except Exception:
        return {"total_trades": 0}


def get_log_lines(n=50):
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", f"-{n}", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip().split("\n")
    except subprocess.CalledProcessError:
        return []


def get_recent_activity(max_lines=15):
    if not os.path.exists(LOG_FILE):
        return []
    try:
        out = subprocess.check_output(
            ["tail", "-500", LOG_FILE], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return []
    keywords = ["SIGNAL:", "LOCKED", "bracket order", "Force close",
                "Journal: ENTRY", "Journal: EXIT", "Authenticated",
                "Bot starting", "Bot stopped", "DRAWDOWN", "DAILY LOSS"]
    items = []
    for line in out.strip().split("\n"):
        if any(kw in line for kw in keywords):
            items.append(line.strip())
    return items[-max_lines:]


def run_systemctl(action):
    if action not in ("start", "stop", "restart"):
        return False, "Invalid action"
    try:
        subprocess.check_output(
            ["systemctl", action, SERVICE_NAME],
            text=True, stderr=subprocess.STDOUT, timeout=30
        )
        time.sleep(2)
        new_status = get_systemd_status()
        return True, f"Service {action}ed. Status: {new_status}"
    except subprocess.CalledProcessError as e:
        return False, f"systemctl {action} failed: {e.output}"
    except subprocess.TimeoutExpired:
        return False, f"systemctl {action} timed out"


def git_pull_and_restart():
    results = []
    try:
        subprocess.check_output(
            ["systemctl", "stop", SERVICE_NAME],
            text=True, stderr=subprocess.STDOUT, timeout=15
        )
        results.append("Bot stopped.")
    except Exception as e:
        results.append(f"Stop warning: {e}")
    try:
        out = subprocess.check_output(
            ["git", "-C", BOT_DIR, "pull"],
            text=True, stderr=subprocess.STDOUT, timeout=30
        )
        results.append(f"Git pull: {out.strip()}")
    except Exception as e:
        results.append(f"Git pull failed: {e}")
        return False, "\n".join(results)
    try:
        subprocess.check_output(
            ["systemctl", "start", SERVICE_NAME],
            text=True, stderr=subprocess.STDOUT, timeout=15
        )
        time.sleep(2)
        results.append(f"Bot restarted. Status: {get_systemd_status()}")
    except Exception as e:
        results.append(f"Start failed: {e}")
        return False, "\n".join(results)
    return True, "\n".join(results)


# ── Command execution ────────────────────────────────────


def collect_full_status():
    """Collect complete bot status."""
    running, pid = is_bot_running()
    return {
        "bot": {"running": running, "pid": pid, "systemd": get_systemd_status()},
        "trading": get_last_status(),
        "token": get_token_status(),
        "journal": get_journal_summary(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def execute_command(cmd_data):
    """Execute a command and return the result."""
    command = cmd_data.get("command", "").lower()
    args = cmd_data.get("args", {})
    log.info("Executing command: %s", command)

    if command == "ping":
        return {"ok": True, "message": "pong", "time": datetime.now(timezone.utc).isoformat()}

    elif command == "status":
        return {"ok": True, "data": collect_full_status()}

    elif command == "logs":
        n = args.get("lines", 50)
        lines = get_log_lines(min(n, 200))
        return {"ok": True, "data": {"lines": lines, "count": len(lines)}}

    elif command == "activity":
        activity = get_recent_activity()
        return {"ok": True, "data": {"activity": activity}}

    elif command == "start":
        ok, msg = run_systemctl("start")
        return {"ok": ok, "message": msg, "data": collect_full_status()}

    elif command == "stop":
        ok, msg = run_systemctl("stop")
        return {"ok": ok, "message": msg, "data": collect_full_status()}

    elif command == "restart":
        ok, msg = run_systemctl("restart")
        return {"ok": ok, "message": msg, "data": collect_full_status()}

    elif command == "update":
        ok, msg = git_pull_and_restart()
        return {"ok": ok, "message": msg}

    elif command == "token":
        return {"ok": True, "data": get_token_status()}

    else:
        return {"ok": False, "message": f"Unknown command: {command}"}


# ── Write status to file ────────────────────────────────


def write_status(result, command_id):
    """Write execution result to .bot_status.json."""
    status = {
        "command_id": command_id,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2, default=str)
    log.info("Status written for command %s", command_id)


def clear_command():
    """Clear the command file after execution."""
    empty = {"command": "", "command_id": "", "sent_at": ""}
    with open(COMMAND_FILE, "w") as f:
        json.dump(empty, f, indent=2)


# ── Main loop ────────────────────────────────────────────


def poll_once():
    """Pull from GitHub, check for command, execute, push result."""
    pull()

    if not os.path.exists(COMMAND_FILE):
        return False

    try:
        with open(COMMAND_FILE) as f:
            cmd_data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return False

    command = cmd_data.get("command", "").strip()
    command_id = cmd_data.get("command_id", "")

    if not command:
        return False

    # Check if we already executed this command
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                last = json.load(f)
            if last.get("command_id") == command_id:
                return False  # Already executed
        except Exception:
            pass

    # Execute
    result = execute_command(cmd_data)
    write_status(result, command_id)
    clear_command()

    # Push result back
    git("add", STATUS_FILE, COMMAND_FILE)
    git("commit", "-m", f"Agent: {command} result")
    push_status()
    log.info("Result pushed for: %s", command)
    return True


def auto_status_update():
    """Push periodic status update even without commands."""
    result = {"ok": True, "data": collect_full_status()}
    status = {
        "command_id": "auto",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2, default=str)
    git("add", STATUS_FILE)
    git("commit", "-m", "Agent: auto status update")
    push_status()


def main():
    once = "--once" in sys.argv

    log.info("VPS Agent starting | branch=%s | poll=%ds", get_branch(), POLL_INTERVAL)

    if once:
        poll_once()
        return

    # Initialize: push current status immediately
    auto_status_update()
    last_auto = time.time()

    while True:
        try:
            had_command = poll_once()

            # Auto status update every 5 minutes
            if time.time() - last_auto > 300:
                auto_status_update()
                last_auto = time.time()

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log.info("Agent stopped.")
            break
        except Exception as e:
            log.error("Agent error: %s", e)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
