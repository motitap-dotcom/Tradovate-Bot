#!/bin/bash
# Auto-sync repo on every Claude Code session start
cd "$(git rev-parse --show-toplevel)" 2>/dev/null || exit 0

echo "Syncing repository..."

# Fetch all remotes
git fetch origin 2>/dev/null

# Pull latest changes for current branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git pull origin "$BRANCH" --ff-only 2>/dev/null

echo "Repo synced — branch: $BRANCH"
exit 0
