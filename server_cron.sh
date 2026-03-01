#!/bin/bash
# server_cron.sh — Deploy script for server cron
# Pulls latest code, restarts bot, reports status back to repo.
#
# Usage: Add to crontab:
#   */5 * * * * cd /root/tradovate-bot && bash server_cron.sh >> /var/log/tradovate-cron.log 2>&1
#
set -euo pipefail

BOT_DIR="${BOT_DIR:-/root/tradovate-bot}"
SERVICE="tradovate-bot"
STATUS_FILE="server_status.json"

cd "$BOT_DIR"

# ── 0. Auto-detect deploy branch: prefer main, fall back to old branch ──
if [ -n "${DEPLOY_BRANCH:-}" ]; then
    BRANCH="$DEPLOY_BRANCH"
elif git ls-remote --heads origin main 2>/dev/null | grep -q main; then
    BRANCH="main"
    # Switch local checkout to main if needed
    CURRENT=$(git branch --show-current)
    if [ "$CURRENT" != "main" ]; then
        echo "[$(date)] Switching from $CURRENT to main..."
        git fetch origin main
        git checkout -B main origin/main
    fi
else
    BRANCH="claude/tradovate-api-research-DPnl9"
fi

# ── 1. Pull latest code ──
echo "[$(date)] Checking for updates on $BRANCH..."
git fetch origin "$BRANCH" 2>/dev/null

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$LOCAL")

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[$(date)] New code found. Updating..."
    git reset --hard "origin/$BRANCH"
    echo "[$(date)] Now at: $(git log -1 --oneline)"

    # Update dependencies if needed
    if [ -f venv/bin/pip ]; then
        venv/bin/pip install -r requirements.txt -q 2>&1 | tail -3
    fi

    # Restart bot
    echo "[$(date)] Restarting bot..."
    systemctl restart "$SERVICE"
    sleep 5
else
    echo "[$(date)] No changes."
fi

# ── 1b. Auto-heal: restart bot if it's not running ──
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "[$(date)] Bot is DOWN — attempting auto-restart..."
    systemctl restart "$SERVICE"
    sleep 5
    if systemctl is-active --quiet "$SERVICE"; then
        echo "[$(date)] Auto-restart SUCCEEDED."
    else
        echo "[$(date)] Auto-restart FAILED. Check: journalctl -u $SERVICE -n 50"
    fi
fi

# ── 2. Collect status ──
BOT_ACTIVE="false"
BOT_PID=""
BOT_UPTIME=""
LAST_LOG=""

if systemctl is-active --quiet "$SERVICE"; then
    BOT_ACTIVE="true"
    BOT_PID=$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || echo "")
    # Get uptime from systemd
    ACTIVE_ENTER=$(systemctl show "$SERVICE" --property=ActiveEnterTimestamp --value 2>/dev/null || echo "")
    if [ -n "$ACTIVE_ENTER" ]; then
        BOT_UPTIME="$ACTIVE_ENTER"
    fi
fi

# Last 5 log lines
LAST_LOG=$(journalctl -u "$SERVICE" --no-pager -n 5 2>/dev/null | tail -5 || echo "no logs")

# live_status.json from bot (written every 30s)
LIVE_STATUS="{}"
if [ -f "$BOT_DIR/live_status.json" ]; then
    LIVE_STATUS=$(cat "$BOT_DIR/live_status.json" 2>/dev/null || echo "{}")
fi

# ── 3. Write server_status.json ──
cat > "$STATUS_FILE" <<STATUSEOF
{
  "timestamp": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "server_time": "$(date '+%Y-%m-%d %H:%M:%S %Z')",
  "bot_active": $BOT_ACTIVE,
  "bot_pid": "$BOT_PID",
  "bot_uptime_since": "$BOT_UPTIME",
  "git_commit": "$(git log -1 --format='%h %s')",
  "git_branch": "$(git branch --show-current)",
  "code_updated": $([ "$LOCAL" != "$REMOTE" ] && echo "true" || echo "false"),
  "last_log": $(echo "$LAST_LOG" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))" 2>/dev/null || echo '""'),
  "live_status": $LIVE_STATUS
}
STATUSEOF

echo "[$(date)] Status: bot_active=$BOT_ACTIVE, pid=$BOT_PID"

# ── 4. Push status back to repo ──
git add "$STATUS_FILE"

if git diff --cached --quiet; then
    echo "[$(date)] No status change to push."
else
    git config user.name "tradovate-bot-server" 2>/dev/null || true
    git config user.email "bot@server" 2>/dev/null || true
    git commit -m "bot-status: $(date -u '+%Y-%m-%d %H:%M UTC') | active=$BOT_ACTIVE" --no-verify
    git push origin "$BRANCH" 2>/dev/null && echo "[$(date)] Status pushed." || echo "[$(date)] Push failed (will retry next cycle)."
fi

echo "[$(date)] Done."
