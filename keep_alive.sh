#!/bin/bash
# ============================================================
# keep_alive.sh — Ensures the bot NEVER stays down
# ============================================================
# This script wraps bot.py with infinite retry logic.
# Use this if systemd is not available or as extra safety.
#
# Usage:
#   nohup bash keep_alive.sh >> /var/log/tradovate-keepalive.log 2>&1 &
#   # or via cron:
#   * * * * * cd /root/tradovate-bot && bash keep_alive.sh >> /var/log/tradovate-keepalive.log 2>&1
# ============================================================

set -uo pipefail

BOT_DIR="${BOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
PIDFILE="$BOT_DIR/.bot.pid"
LOCKFILE="$BOT_DIR/.keepalive.lock"
MAX_RESTART_DELAY=120   # Max seconds between restarts (caps exponential backoff)
RESTART_DELAY=10        # Initial restart delay

cd "$BOT_DIR" || { echo "[$(date)] FATAL: Cannot cd to $BOT_DIR. Exiting."; exit 1; }

# Prevent multiple keep_alive instances
exec 200>"$LOCKFILE"
flock -n 200 || { echo "[$(date)] Another keep_alive is already running. Exiting."; exit 0; }

cleanup() {
    rm -f "$PIDFILE"
    exit 0
}
trap cleanup EXIT INT TERM

echo "[$(date)] keep_alive.sh started in $BOT_DIR"

# Activate venv if available
if [ -f "$BOT_DIR/venv/bin/activate" ]; then
    source "$BOT_DIR/venv/bin/activate"
fi

consecutive_failures=0

while true; do
    # Check if systemd is managing the bot — defer to it
    if systemctl is-active --quiet tradovate-bot 2>/dev/null; then
        echo "[$(date)] Bot is managed by systemd. Sleeping 5 min..."
        sleep 300
        continue
    fi

    # Check if bot is already running (from another process)
    if [ -f "$PIDFILE" ]; then
        OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            # Bot is running, wait and check again
            sleep 60
            continue
        fi
        # Stale PID file
        rm -f "$PIDFILE"
    fi

    echo "[$(date)] Starting bot.py (attempt after $consecutive_failures failures)..."
    python3 -u bot.py &
    BOT_PID=$!
    echo "$BOT_PID" > "$PIDFILE"
    echo "[$(date)] Bot started with PID $BOT_PID"

    # Wait for bot to exit
    wait $BOT_PID
    EXIT_CODE=$?
    rm -f "$PIDFILE"

    echo "[$(date)] Bot exited with code $EXIT_CODE"

    if [ $EXIT_CODE -eq 0 ]; then
        # Clean exit (end of trading session) — short delay then restart for next session
        consecutive_failures=0
        RESTART_DELAY=10
        echo "[$(date)] Clean exit. Restarting in 10s for next session..."
        sleep 10
    else
        # Crash — exponential backoff
        consecutive_failures=$((consecutive_failures + 1))
        RESTART_DELAY=$((RESTART_DELAY * 2))
        if [ $RESTART_DELAY -gt $MAX_RESTART_DELAY ]; then
            RESTART_DELAY=$MAX_RESTART_DELAY
        fi
        echo "[$(date)] CRASH (failures=$consecutive_failures). Restarting in ${RESTART_DELAY}s..."
        sleep $RESTART_DELAY
    fi
done
