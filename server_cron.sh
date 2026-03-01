#!/bin/bash
# server_cron.sh — Pulls latest code, restarts bot, pushes status via GitHub API.
#
# Required env var:
#   GH_PAT — GitHub Personal Access Token (repo scope) for pushing status
#
# Auto-installed by deploy workflow. Manual setup:
#   */5 * * * * cd /root/tradovate-bot && . .gh_pat 2>/dev/null; bash server_cron.sh >> /var/log/tradovate-cron.log 2>&1
#
set -euo pipefail

BOT_DIR="${BOT_DIR:-/root/tradovate-bot}"
SERVICE="tradovate-bot"
BRANCH="${DEPLOY_BRANCH:-main}"
STATUS_FILE="server_status.json"

cd "$BOT_DIR"

# Auto-detect repo from git remote
GITHUB_REPO="${GITHUB_REPO:-$(git remote get-url origin | sed -E 's|.*github\.com[:/]||; s|\.git$||')}"

# ── 1. Pull latest code ──
echo "[$(date)] Checking for updates on $BRANCH..."
git fetch origin "$BRANCH" 2>/dev/null || echo "[$(date)] Fetch failed"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$LOCAL")
CODE_UPDATED="false"

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[$(date)] New code found. Updating..."
    git stash --include-untracked 2>/dev/null || true
    git reset --hard "origin/$BRANCH"
    echo "[$(date)] Now at: $(git log -1 --oneline)"
    CODE_UPDATED="true"

    # Update dependencies
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

# ── 2b. Run connect_verify.py (quick mode) for deep health check ──
CONNECT_STATUS="{}"
if [ -f "$BOT_DIR/connect_verify.py" ]; then
    echo "[$(date)] Running connection verifier..."
    PYTHON="${BOT_DIR}/venv/bin/python"
    [ ! -f "$PYTHON" ] && PYTHON="python3"
    timeout 45 "$PYTHON" "$BOT_DIR/connect_verify.py" --quick >> /var/log/tradovate-cron.log 2>&1 || true
    [ -f "$BOT_DIR/connect_status.json" ] && CONNECT_STATUS=$(cat "$BOT_DIR/connect_status.json" 2>/dev/null || echo "{}")
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
  "connect_verify": $CONNECT_STATUS
}
STATUSEOF

echo "[$(date)] Status: bot_active=$BOT_ACTIVE, pid=$BOT_PID"

# ── 4. Push status to GitHub via API (directly to main, no git push needed) ──
if [ -z "${GH_PAT:-}" ]; then
    echo "[$(date)] No GH_PAT set — status written locally only."
    exit 0
fi

COMMIT_MSG="bot-status: $(date -u '+%Y-%m-%d %H:%M UTC') | active=$BOT_ACTIVE"

# Push a file to GitHub via Contents API
push_file_to_github() {
    local file_path="$1"
    local commit_msg="$2"
    local api_url="https://api.github.com/repos/$GITHUB_REPO/contents/$file_path"

    # Get current file SHA on main (required for updates, empty for first create)
    local file_sha
    file_sha=$(curl -sf -H "Authorization: token $GH_PAT" \
      "${api_url}?ref=main" 2>/dev/null | \
      python3 -c "import sys,json; print(json.load(sys.stdin).get('sha',''))" 2>/dev/null || echo "")

    # Build JSON payload via python3 (safe escaping)
    local payload
    payload=$(FILE_PATH="$file_path" COMMIT_MSG_="$commit_msg" FILE_SHA="$file_sha" python3 << 'PYEOF'
import json, base64, os
with open(os.environ['FILE_PATH'], 'rb') as f:
    content = base64.b64encode(f.read()).decode()
payload = {'message': os.environ['COMMIT_MSG_'], 'content': content, 'branch': 'main'}
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
      "$api_url" \
      -d "$payload" > /dev/null 2>&1
}

# Push server_status.json (retry up to 3 times)
PUSH_OK="false"
for i in 1 2 3; do
    if push_file_to_github "$STATUS_FILE" "$COMMIT_MSG"; then
        PUSH_OK="true"
        echo "[$(date)] server_status.json pushed via API (attempt $i)."
        break
    else
        echo "[$(date)] API push attempt $i failed. Retrying in ${i}s..."
        sleep "$i"
    fi
done

# Also push connect_status.json if it exists
if [ -f "$BOT_DIR/connect_status.json" ]; then
    CONNECT_MSG="connect-status: $(date -u '+%Y-%m-%d %H:%M UTC') | active=$BOT_ACTIVE"
    push_file_to_github "connect_status.json" "$CONNECT_MSG" 2>/dev/null && \
        echo "[$(date)] connect_status.json pushed." || \
        echo "[$(date)] connect_status.json push failed."
fi

if [ "$PUSH_OK" = "false" ]; then
    echo "[$(date)] Push failed after 3 attempts. Will retry next cycle."
fi

echo "[$(date)] Done."
