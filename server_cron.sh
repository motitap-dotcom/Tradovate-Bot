#!/bin/bash
# server_cron.sh — Pulls latest code, restarts bot, pushes status via GitHub API.
#
# Required env var:
#   GH_PAT — GitHub Personal Access Token (repo scope) for pushing status
#
# Auto-installed by deploy workflow. Manual setup:
#   */5 * * * * cd /root/tradovate-bot && . .gh_pat 2>/dev/null; bash server_cron.sh >> /var/log/tradovate-cron.log 2>&1
#
# Use set -u (catch unset vars) but NOT set -e (don't exit on failure).
# This cron must ALWAYS reach the auto-heal and status sections, even if
# git fetch or pip fails due to transient network issues.
set -u

BOT_DIR="${BOT_DIR:-/root/tradovate-bot}"
SERVICE="tradovate-bot"
STATUS_FILE="server_status.json"

cd "$BOT_DIR" || { echo "[$(date)] FATAL: $BOT_DIR not found"; exit 1; }

# ── 0. Auto-detect deploy branch: prefer main, fall back to old branch ──
if [ -n "${DEPLOY_BRANCH:-}" ]; then
    BRANCH="$DEPLOY_BRANCH"
elif git ls-remote --heads origin main 2>/dev/null | grep -q main; then
    BRANCH="main"
    # Switch local checkout to main if needed
    CURRENT=$(git branch --show-current 2>/dev/null || echo "")
    if [ -n "$CURRENT" ] && [ "$CURRENT" != "main" ]; then
        echo "[$(date)] Switching from $CURRENT to main..."
        git fetch origin main 2>/dev/null && git checkout -B main origin/main || echo "[$(date)] Warning: failed to switch to main"
    fi
else
    BRANCH="claude/tradovate-api-research-DPnl9"
fi

# Auto-detect repo from git remote
GITHUB_REPO="${GITHUB_REPO:-$(git remote get-url origin | sed -E 's|.*github\.com[:/]||; s|\.git$||')}"

# ── 1. Pull latest code ──
echo "[$(date)] Checking for updates on $BRANCH..."
if git fetch origin "$BRANCH" 2>/dev/null; then
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$LOCAL")
    CODE_UPDATED="false"

    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "[$(date)] New code found. Updating..."
        git reset --hard "origin/$BRANCH"
        echo "[$(date)] Now at: $(git log -1 --oneline)"
        CODE_UPDATED="true"

        # Update dependencies if needed
        if [ -f venv/bin/pip ]; then
            venv/bin/pip install -r requirements.txt -q 2>&1 | tail -3 || true
        fi

        # Restart bot
        echo "[$(date)] Restarting bot..."
        systemctl restart "$SERVICE"
        sleep 5
    else
        echo "[$(date)] No changes."
    fi
else
    echo "[$(date)] Warning: git fetch failed (network issue?). Skipping code update."
    LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    REMOTE="$LOCAL"
    CODE_UPDATED="false"
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

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    BOT_ACTIVE="true"
    BOT_PID=$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || echo "")
    ACTIVE_ENTER=$(systemctl show "$SERVICE" --property=ActiveEnterTimestamp --value 2>/dev/null || echo "")
    [ -n "$ACTIVE_ENTER" ] && BOT_UPTIME="$ACTIVE_ENTER"
fi

LAST_LOG=$(journalctl -u "$SERVICE" --no-pager -n 5 2>/dev/null | tail -5 || echo "no logs")
DISK_USAGE=$(df -h / 2>/dev/null | tail -1 | awk '{print $5}' || echo "unknown")
MEMORY=$(free -m 2>/dev/null | awk '/^Mem:/{printf "%dMB/%dMB (%.0f%%)", $3, $2, $3/$2*100}' || echo "unknown")

LIVE_STATUS="{}"
[ -f "$BOT_DIR/live_status.json" ] && LIVE_STATUS=$(cat "$BOT_DIR/live_status.json" 2>/dev/null || echo "{}")

# ── 2b. Run deep health check (bot_health_check.py preferred, verify_bot.py fallback) ──
HEALTH_DATA="{}"
PYTHON="${BOT_DIR}/venv/bin/python3"
[ ! -f "$PYTHON" ] && PYTHON="python3"

if [ -f "$BOT_DIR/bot_health_check.py" ]; then
    echo "[$(date)] Running health check..."
    $PYTHON "$BOT_DIR/bot_health_check.py" --quick >> /var/log/tradovate-cron.log 2>&1 || true
    [ -f "$BOT_DIR/bot_health.json" ] && HEALTH_DATA=$(cat "$BOT_DIR/bot_health.json" 2>/dev/null || echo "{}")
elif [ -f "$BOT_DIR/verify_bot.py" ]; then
    $PYTHON "$BOT_DIR/verify_bot.py" --server > /dev/null 2>&1 || true
    [ -f "$BOT_DIR/verify_report.json" ] && HEALTH_DATA=$(cat "$BOT_DIR/verify_report.json" 2>/dev/null || echo "{}")
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
  "live_status": $LIVE_STATUS,
  "health_check": $HEALTH_DATA
}
STATUSEOF

echo "[$(date)] Status: bot_active=$BOT_ACTIVE, pid=$BOT_PID"

# ── 4. Push status to GitHub via API (directly to main, no git push needed) ──
if [ -z "${GH_PAT:-}" ]; then
    echo "[$(date)] No GH_PAT set — status written locally only."
    exit 0
fi

# Include health verdict in commit message
HEALTH_VERDICT=$(python3 -c "import json; print(json.load(open('bot_health.json')).get('verdict',{}).get('overall','?'))" 2>/dev/null || echo "?")
COMMIT_MSG="bot-status: $(date -u '+%Y-%m-%d %H:%M UTC') | active=$BOT_ACTIVE | health=$HEALTH_VERDICT"
API_URL="https://api.github.com/repos/$GITHUB_REPO/contents/$STATUS_FILE"

# Push function: fetches current SHA, builds payload, pushes
push_status() {
    # Get current file SHA on main (required for updates, empty for first create)
    local file_sha
    file_sha=$(curl -sf -H "Authorization: token $GH_PAT" \
      "${API_URL}?ref=main" 2>/dev/null | \
      python3 -c "import sys,json; print(json.load(sys.stdin).get('sha',''))" 2>/dev/null || echo "")

    # Build JSON payload via python3 (safe escaping)
    local payload
    payload=$(STATUS_FILE="$STATUS_FILE" COMMIT_MSG="$COMMIT_MSG" FILE_SHA="$file_sha" python3 << 'PYEOF'
import json, base64, os
with open(os.environ['STATUS_FILE'], 'rb') as f:
    content = base64.b64encode(f.read()).decode()
payload = {'message': os.environ['COMMIT_MSG'], 'content': content, 'branch': 'main'}
sha = os.environ.get('FILE_SHA', '')
if sha:
    payload['sha'] = sha
print(json.dumps(payload))
PYEOF
    )

    # PUT to GitHub Contents API
    curl -sf -X PUT \
      -H "Authorization: token $GH_PAT" \
      -H "Accept: application/vnd.github.v3+json" \
      -H "Content-Type: application/json" \
      "$API_URL" \
      -d "$payload" > /dev/null 2>&1
}

# Retry up to 3 times (handles SHA conflicts automatically)
PUSH_OK="false"
for i in 1 2 3; do
    if push_status; then
        PUSH_OK="true"
        echo "[$(date)] Status pushed via API (attempt $i)."
        break
    else
        echo "[$(date)] API push attempt $i failed. Retrying in ${i}s..."
        sleep "$i"
    fi
done

if [ "$PUSH_OK" = "false" ]; then
    echo "[$(date)] Push failed after 3 attempts. Will retry next cycle."
fi

echo "[$(date)] Done."
