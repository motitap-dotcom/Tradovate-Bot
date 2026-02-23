#!/bin/bash
# ============================================================
# Tradovate Bot - VPS Setup Script
# Run on Contabo VPS: bash setup_vps.sh
# ============================================================
set -e

BOT_DIR="/root/tradovate-bot"
REPO_URL="https://github.com/motitap-dotcom/Tradovate-Bot.git"
BRANCH="claude/tradovate-api-research-DPnl9"

echo "========================================="
echo "  Tradovate Bot - VPS Setup"
echo "========================================="

# Check for existing bots
echo ""
echo "[1/6] Checking for existing processes..."
EXISTING=$(ps aux | grep "python.*bot\.py" | grep -v grep | grep -v "tradovate-bot" || true)
if [ -n "$EXISTING" ]; then
    echo "  Found existing bot processes (will NOT touch them):"
    echo "$EXISTING" | sed 's/^/    /'
fi

# Check if already installed
if [ -d "$BOT_DIR" ]; then
    echo ""
    echo "  WARNING: $BOT_DIR already exists!"
    read -p "  Overwrite? (y/N): " CONFIRM
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
        echo "  Aborted."
        exit 0
    fi
    # Kill any running bot from this directory
    pkill -f "$BOT_DIR/bot.py" 2>/dev/null || true
    sleep 1
    rm -rf "$BOT_DIR"
fi

# Install system dependencies
echo ""
echo "[2/6] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl > /dev/null 2>&1
echo "  Done."

# Clone repository
echo ""
echo "[3/6] Cloning repository..."
git clone -b "$BRANCH" "$REPO_URL" "$BOT_DIR" 2>&1 | tail -3
cd "$BOT_DIR"
echo "  Done."

# Install Python dependencies
echo ""
echo "[4/6] Installing Python packages..."
pip3 install -q requests websocket-client python-dotenv numpy 2>&1 | tail -3
# Playwright is optional (only needed for CAPTCHA on first login)
pip3 install -q playwright 2>/dev/null && playwright install chromium --with-deps 2>/dev/null || echo "  Playwright skipped (optional - only for first-time CAPTCHA login)"
echo "  Done."

# Create .env file
echo ""
echo "[5/6] Creating .env configuration..."
cat > "$BOT_DIR/.env" << 'ENVEOF'
TRADOVATE_USERNAME=FNFTMOTITAPWnBks
TRADOVATE_PASSWORD=hurIQ97##
TRADOVATE_APP_ID=
TRADOVATE_CID=0
TRADOVATE_SECRET=
TRADOVATE_DEVICE_ID=tradovate-bot-vps
TRADOVATE_ENV=demo
PROP_FIRM=fundednext
LOG_LEVEL=INFO
LOG_FILE=bot.log
ENVEOF
chmod 600 "$BOT_DIR/.env"
echo "  Created $BOT_DIR/.env"

# Remove any stale token (will re-authenticate fresh)
rm -f "$BOT_DIR/.tradovate_token.json"

# Start the bot
echo ""
echo "[6/6] Starting bot..."
cd "$BOT_DIR"
nohup python3 bot.py > /dev/null 2>&1 &
BOT_PID=$!
echo "  Bot started with PID: $BOT_PID"

# Wait and verify
sleep 10
if ps -p $BOT_PID > /dev/null 2>&1; then
    echo ""
    echo "========================================="
    echo "  SUCCESS! Bot is running."
    echo "========================================="
    echo ""
    echo "  Directory: $BOT_DIR"
    echo "  PID: $BOT_PID"
    echo "  Log: tail -f $BOT_DIR/bot.log"
    echo ""
    echo "  Last log lines:"
    tail -5 "$BOT_DIR/bot.log" 2>/dev/null | sed 's/^/    /'
    echo ""
    echo "  Commands:"
    echo "    tail -f $BOT_DIR/bot.log    # Watch logs"
    echo "    kill $BOT_PID               # Stop bot"
    echo "    cd $BOT_DIR && nohup python3 bot.py > /dev/null 2>&1 &  # Restart"
else
    echo ""
    echo "  ERROR: Bot exited! Check log:"
    tail -20 "$BOT_DIR/bot.log" 2>/dev/null | sed 's/^/    /'
    exit 1
fi
