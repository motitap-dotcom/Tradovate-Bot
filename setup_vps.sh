#!/bin/bash
# ============================================================
# Tradovate Bot — VPS Auto-Installer (Ubuntu/Debian)
# ============================================================
# ONE COMMAND to install and run the bot:
#
#   sudo PASS='your_password' bash setup_vps.sh
#
# Or interactive (will ask for password):
#
#   sudo bash setup_vps.sh
#
# What it does:
#   1. Installs Python3 + system dependencies
#   2. Clones the repo (or updates if exists)
#   3. Creates Python venv + installs packages
#   4. Creates .env config with your credentials
#   5. Installs systemd service (auto-start on boot)
#   6. Starts the bot automatically
# ============================================================
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }
step() { echo -e "\n${CYAN}── $1 ──${NC}"; }

# ── Config ──────────────────────────────────────────────────
BOT_DIR="/root/tradovate-bot"
REPO_URL="https://github.com/motitap-dotcom/Tradovate-Bot.git"
BRANCH="master"
SERVICE_NAME="tradovate-bot"
USERNAME="FNFTMOTITAPWnBks"

# ── Banner ──────────────────────────────────────────────────
echo -e "${CYAN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Tradovate Bot — VPS Installer v2.0     ║"
echo "  ║   Automatic Setup — Zero Config          ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Root check ──────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    err "Run with sudo:  sudo bash setup_vps.sh"
fi

# ── Get password ────────────────────────────────────────────
# Accept via PASS env var, or ask interactively
if [ -n "${PASS:-}" ]; then
    TRADOVATE_PASS="$PASS"
    log "Password received from environment."
elif [ -f "$BOT_DIR/.env" ] && grep -q "TRADOVATE_PASSWORD=" "$BOT_DIR/.env" 2>/dev/null; then
    EXISTING_PASS=$(grep "^TRADOVATE_PASSWORD=" "$BOT_DIR/.env" | cut -d'=' -f2-)
    if [ -n "$EXISTING_PASS" ] && [ "$EXISTING_PASS" != "your_password_here" ]; then
        TRADOVATE_PASS="$EXISTING_PASS"
        log "Using existing password from .env"
    else
        echo -e "${BOLD}Enter your Tradovate password:${NC}"
        read -r -s TRADOVATE_PASS
        echo ""
    fi
else
    echo -e "${BOLD}Enter your Tradovate password:${NC}"
    read -r -s TRADOVATE_PASS
    echo ""
fi

if [ -z "$TRADOVATE_PASS" ]; then
    err "Password cannot be empty!"
fi

# ── Accept optional access token ────────────────────────────
# If CAPTCHA blocks login, user can paste a token from browser DevTools
TOKEN="${TOKEN:-}"

# ── 1. System packages ─────────────────────────────────────
step "1/7  Installing system packages"
apt-get update -qq 2>/dev/null
apt-get install -y -qq python3 python3-pip python3-venv git curl jq > /dev/null 2>&1
log "System packages OK"

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log "Python $PY_VER"

# ── 2. Clone or update repo ────────────────────────────────
step "2/7  Getting latest code"

# Stop bot if running
systemctl stop "$SERVICE_NAME" 2>/dev/null || true

if [ -d "$BOT_DIR/.git" ]; then
    log "Updating existing installation..."
    cd "$BOT_DIR"
    # Save .env and token before update
    [ -f .env ] && cp .env /tmp/.tradovate_env_backup 2>/dev/null || true
    [ -f .tradovate_token.json ] && cp .tradovate_token.json /tmp/.tradovate_token_backup 2>/dev/null || true
    git fetch origin "$BRANCH" 2>&1 | tail -1
    git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH" 2>/dev/null || true
    git reset --hard "origin/$BRANCH"
    # Restore .env and token
    [ -f /tmp/.tradovate_env_backup ] && cp /tmp/.tradovate_env_backup .env 2>/dev/null || true
    [ -f /tmp/.tradovate_token_backup ] && cp /tmp/.tradovate_token_backup .tradovate_token.json 2>/dev/null || true
    log "Code updated."
else
    if [ -d "$BOT_DIR" ]; then
        warn "$BOT_DIR exists but is not a git repo. Backing up..."
        mv "$BOT_DIR" "${BOT_DIR}.bak.$(date +%s)"
    fi
    log "Cloning repository..."
    git clone -b "$BRANCH" "$REPO_URL" "$BOT_DIR" 2>&1 | tail -3
    cd "$BOT_DIR"
    log "Repository cloned."
fi

cd "$BOT_DIR"

# ── 3. Python virtual environment ──────────────────────────
step "3/7  Python environment"
python3 -m venv "$BOT_DIR/venv"
source "$BOT_DIR/venv/bin/activate"

log "Installing dependencies..."
pip install --upgrade pip -q 2>&1 | tail -1
pip install -r "$BOT_DIR/requirements.txt" -q 2>&1 | tail -1

# Playwright (for CAPTCHA bypass)
pip install playwright -q 2>/dev/null || true
playwright install chromium --with-deps 2>/dev/null || warn "Playwright skipped (optional — only for CAPTCHA)"

deactivate
log "Python environment ready."

# ── 4. Create .env with credentials ────────────────────────
step "4/7  Configuration"
ENV_FILE="$BOT_DIR/.env"

cat > "$ENV_FILE" << ENVEOF
# Tradovate Bot Configuration
# Auto-generated by setup_vps.sh

# Tradovate Credentials
TRADOVATE_USERNAME=$USERNAME
TRADOVATE_PASSWORD=$TRADOVATE_PASS
TRADOVATE_APP_ID=
TRADOVATE_CID=0
TRADOVATE_SECRET=
TRADOVATE_DEVICE_ID=tradovate-bot-vps

# Manual token (optional — only if CAPTCHA blocks login)
TRADOVATE_ACCESS_TOKEN=$TOKEN

# Environment: demo or live
TRADOVATE_ENV=demo

# Prop Firm
PROP_FIRM=fundednext
TRADOVATE_ORGANIZATION=

# Logging
LOG_LEVEL=INFO
LOG_FILE=$BOT_DIR/bot.log
ENVEOF

chmod 600 "$ENV_FILE"
log ".env created with your credentials."

# ── 5. Systemd service ─────────────────────────────────────
step "5/7  Systemd service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Tradovate Trading Bot
After=network-online.target
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python bot.py --live
EnvironmentFile=$BOT_DIR/.env
Environment=PYTHONUNBUFFERED=1

# Auto-restart on crash (30s delay)
Restart=on-failure
RestartSec=30

# Log to file + journal
StandardOutput=append:$BOT_DIR/bot.log
StandardError=append:$BOT_DIR/bot.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" > /dev/null 2>&1
log "Service installed and enabled (auto-start on boot)."

# ── 6. Log rotation ────────────────────────────────────────
step "6/7  Log rotation"
cat > "/etc/logrotate.d/$SERVICE_NAME" << EOF
$BOT_DIR/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
log "Log rotation configured."

# ── 7. Helper script (bot-ctl) ─────────────────────────────
step "7/7  Helper commands"
cat > "$BOT_DIR/bot-ctl.sh" << 'CTLEOF'
#!/bin/bash
# Tradovate Bot — Quick Control
SERVICE="tradovate-bot"
BOT_DIR="/root/tradovate-bot"

case "${1:-help}" in
    start)
        sudo systemctl start "$SERVICE"
        echo "Bot started. Use './bot-ctl.sh logs' to watch."
        ;;
    stop)
        sudo systemctl stop "$SERVICE"
        echo "Bot stopped."
        ;;
    restart)
        sudo systemctl restart "$SERVICE"
        echo "Bot restarted."
        ;;
    status)
        echo "=== Service Status ==="
        sudo systemctl is-active "$SERVICE" 2>/dev/null || true
        echo ""
        echo "=== Last 20 Log Lines ==="
        tail -20 "$BOT_DIR/bot.log" 2>/dev/null || echo "(no logs yet)"
        ;;
    logs)
        tail -f "$BOT_DIR/bot.log"
        ;;
    update)
        echo "Updating bot..."
        sudo systemctl stop "$SERVICE" 2>/dev/null || true
        cd "$BOT_DIR"
        git pull origin "$(git branch --show-current)"
        source venv/bin/activate
        pip install -r requirements.txt -q
        deactivate
        sudo systemctl start "$SERVICE"
        echo "Updated and restarted!"
        ;;
    *)
        echo "Tradovate Bot Control"
        echo ""
        echo "  ./bot-ctl.sh start    - Start the bot"
        echo "  ./bot-ctl.sh stop     - Stop the bot"
        echo "  ./bot-ctl.sh restart  - Restart the bot"
        echo "  ./bot-ctl.sh status   - Show status + recent logs"
        echo "  ./bot-ctl.sh logs     - Watch live logs"
        echo "  ./bot-ctl.sh update   - Pull latest code & restart"
        ;;
esac
CTLEOF
chmod +x "$BOT_DIR/bot-ctl.sh"

# ── Start the bot! ──────────────────────────────────────────
echo ""
echo -e "${CYAN}════════════════════════════════════════════════${NC}"
log "Starting the bot..."
systemctl start "$SERVICE_NAME"
sleep 3

# Check if it started OK
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo -e "${GREEN}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║         BOT IS RUNNING!                  ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
else
    echo -e "${YELLOW}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║   Bot started but may have an issue.     ║"
    echo "  ║   Check logs below:                      ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
fi

echo ""
echo -e "  ${CYAN}Useful commands:${NC}"
echo ""
echo -e "    ${GREEN}cd $BOT_DIR${NC}"
echo -e "    ${GREEN}./bot-ctl.sh status${NC}    Check if bot is running"
echo -e "    ${GREEN}./bot-ctl.sh logs${NC}      Watch live logs"
echo -e "    ${GREEN}./bot-ctl.sh restart${NC}   Restart the bot"
echo -e "    ${GREEN}./bot-ctl.sh update${NC}    Pull new code & restart"
echo ""

# Show last few lines of log
echo -e "${CYAN}── Recent log output ──${NC}"
sleep 2
tail -15 "$BOT_DIR/bot.log" 2>/dev/null || echo "(waiting for first log lines...)"
echo ""
echo -e "${GREEN}Done! Bot is running in the background.${NC}"
echo ""
