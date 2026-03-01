#!/bin/bash
# server_cron.sh — Deploy script for server cron
# Pulls latest code, restarts bot, reports status back to repo.
#
# Required env vars (set in crontab or /etc/environment):
#   GH_PAT          — GitHub Personal Access Token (repo scope) for push
#   GITHUB_REPO     — e.g. "motitap-dotcom/Tradovate-Bot" (optional, auto-detected)
#
# Usage: Add to crontab:
#   */5 * * * * cd /root/tradovate-bot && bash server_cron.sh >> /var/log/tradovate-cron.log 2>&1
#
# Or with env vars inline:
#   */5 * * * * GH_PAT=ghp_xxx cd /root/tradovate-bot && bash server_cron.sh >> /var/log/tradovate-cron.log 2>&1
#
set -euo pipefail

BOT_DIR="${BOT_DIR:-/root/tradovate-bot}"
SERVICE="tradovate-bot"
BRANCH="${DEPLOY_BRANCH:-main}"
STATUS_FILE="server_status.json"
STATUS_BRANCH="${STATUS_BRANCH:-main}"

cd "$BOT_DIR"

# ── 0. Configure git auth (GitHub PAT) ──
if [ -n "${GH_PAT:-}" ]; then
    # Auto-detect repo from remote URL if not set
    if [ -z "${GITHUB_REPO:-}" ]; then
        GITHUB_REPO=$(git remote get-url origin | sed -E 's|.*github\.com[:/]||; s|\.git$||')
    fi
    # Set push URL with token auth
    git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPO}.git" 2>/dev/null || true
fi

git config user.name "tradovate-bot-server" 2>/dev/null || true
git config user.email "bot@server" 2>/dev/null || true

# ── 1. Pull latest code ──
echo "[$(date)] Checking for updates on $BRANCH..."
git fetch origin "$BRANCH" 2>/dev/null || { echo "[$(date)] Fetch failed"; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$LOCAL")
CODE_UPDATED="false"

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[$(date)] New code found. Updating..."
    git stash --include-untracked 2>/dev/null || true
    git reset --hard "origin/$BRANCH"
    echo "[$(date)] Now at: $(git log -1 --oneline)"
    CODE_UPDATED="true"

    # Update dependencies if needed
    if [ -f venv/bin/pip ]; then
        venv/bin/pip install -r requirements.txt -q 2>&1 | tail -3
    fi

    # Restart bot
    echo "[$(date)] Restarting bot..."
    systemctl restart "$SERVICE" 2>/dev/null || echo "[$(date)] systemctl restart failed"
    sleep 5
else
    echo "[$(date)] No changes."
fi

# ── 2. Collect status ──
BOT_ACTIVE="false"
BOT_PID=""
BOT_UPTIME=""
LAST_LOG=""
DISK_USAGE=""
MEMORY=""

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    BOT_ACTIVE="true"
    BOT_PID=$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || echo "")
    ACTIVE_ENTER=$(systemctl show "$SERVICE" --property=ActiveEnterTimestamp --value 2>/dev/null || echo "")
    if [ -n "$ACTIVE_ENTER" ]; then
        BOT_UPTIME="$ACTIVE_ENTER"
    fi
fi

# Last 5 log lines
LAST_LOG=$(journalctl -u "$SERVICE" --no-pager -n 5 2>/dev/null | tail -5 || echo "no logs")

# System info
DISK_USAGE=$(df -h / 2>/dev/null | tail -1 | awk '{print $5}' || echo "unknown")
MEMORY=$(free -m 2>/dev/null | awk '/^Mem:/{printf "%dMB/%dMB (%.0f%%)", $3, $2, $3/$2*100}' || echo "unknown")

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
  "git_branch": "$(git branch --show-current 2>/dev/null || echo 'detached')",
  "code_updated": $CODE_UPDATED,
  "disk_usage": "$DISK_USAGE",
  "memory": "$MEMORY",
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
    git commit -m "bot-status: $(date -u '+%Y-%m-%d %H:%M UTC') | active=$BOT_ACTIVE" --no-verify

    # Push with retry (up to 3 attempts)
    PUSH_OK="false"
    for i in 1 2 3; do
        if git push origin HEAD:"$STATUS_BRANCH" 2>&1; then
            PUSH_OK="true"
            echo "[$(date)] Status pushed (attempt $i)."
            break
        else
            echo "[$(date)] Push attempt $i failed. Retrying in ${i}s..."
            sleep "$i"
        fi
    done

    if [ "$PUSH_OK" = "false" ]; then
        echo "[$(date)] Push failed after 3 attempts. Will retry next cycle."
    fi
fi

echo "[$(date)] Done."
