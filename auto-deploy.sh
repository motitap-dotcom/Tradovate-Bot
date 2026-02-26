#!/bin/bash
# Auto-deploy: pulls latest code from GitHub and restarts the bot
# Run via cron every 2 minutes: */2 * * * * /root/Tradovate-Bot/auto-deploy.sh

REPO_DIR="/root/Tradovate-Bot"
LOG="$REPO_DIR/deploy.log"
LOCK="/tmp/tradovate-deploy.lock"

# Prevent concurrent runs
if [ -f "$LOCK" ]; then
    exit 0
fi
trap "rm -f $LOCK" EXIT
touch "$LOCK"

cd "$REPO_DIR" || exit 1

# Fetch latest from GitHub
git fetch origin 2>>"$LOG"

# Check if there are new commits
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main 2>/dev/null || git rev-parse origin/master 2>/dev/null)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0  # Already up to date
fi

# New code available — pull and restart
echo "$(date): Deploying $LOCAL -> $REMOTE" >> "$LOG"
git merge "$REMOTE" --no-edit >> "$LOG" 2>&1

if [ $? -eq 0 ]; then
    echo "$(date): Restarting tradovate-bot" >> "$LOG"
    systemctl restart tradovate-bot
    echo "$(date): Deploy complete" >> "$LOG"
else
    echo "$(date): Merge failed!" >> "$LOG"
fi
