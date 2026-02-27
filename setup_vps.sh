#!/bin/bash
# ============================================================
# Tradovate Bot — VPS Auto-Installer (Ubuntu/Debian)
# ============================================================
# Usage:
#   chmod +x setup_vps.sh
#   sudo bash setup_vps.sh
#
# What it does:
#   1. Installs Python3 + system dependencies
#   2. Clones the repo (or updates if exists)
#   3. Creates Python venv + installs packages
#   4. Creates .env config (you must edit credentials)
#   5. Installs systemd service (auto-start on boot)
#   6. Sets up log rotation
# ============================================================
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

# ── Config ──────────────────────────────────────────────────
BOT_DIR="/root/tradovate-bot"
REPO_URL="https://github.com/motitap-dotcom/Tradovate-Bot.git"
BRANCH="claude/tradovate-api-research-DPnl9"
SERVICE_NAME="tradovate-bot"

echo -e "${CYAN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Tradovate Bot — VPS Installer          ║"
echo "  ║   Ubuntu / Debian                        ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. System packages ─────────────────────────────────────
log "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl jq > /dev/null 2>&1
log "System packages installed."

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log "Python version: $PY_VER"

# ── 2. Clone or update repo ────────────────────────────────
if [ -d "$BOT_DIR/.git" ]; then
    log "Updating existing installation..."
    cd "$BOT_DIR"
    # Stop bot if running
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    git fetch origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
    log "Repository updated."
else
    if [ -d "$BOT_DIR" ]; then
        warn "$BOT_DIR exists but is not a git repo. Backing up..."
        mv "$BOT_DIR" "${BOT_DIR}.bak.$(date +%s)"
    fi
    log "Cloning repository..."
    git clone -b "$BRANCH" "$REPO_URL" "$BOT_DIR" 2>&1 | tail -3
    log "Repository cloned."
fi

cd "$BOT_DIR"

# ── 3. Python virtual environment ──────────────────────────
log "Setting up Python virtual environment..."
python3 -m venv "$BOT_DIR/venv"
source "$BOT_DIR/venv/bin/activate"

log "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r "$BOT_DIR/requirements.txt" -q

# Playwright (optional — only needed for CAPTCHA bypass)
pip install playwright -q 2>/dev/null || true
playwright install chromium --with-deps 2>/dev/null || warn "Playwright skipped (optional — only needed for first-time CAPTCHA login)"

deactivate
log "Python environment ready."

# ── 4. Environment file ────────────────────────────────────
ENV_FILE="$BOT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    warn "Creating .env file — EDIT IT with your credentials!"
    cat > "$ENV_FILE" << 'ENVEOF'
# ─── Tradovate Bot Configuration ───────────────────────────
# IMPORTANT: Edit these values with your actual credentials!

# Tradovate Credentials
TRADOVATE_USERNAME=FNFTMOTITAPWnBks
TRADOVATE_PASSWORD=your_password_here
TRADOVATE_APP_ID=
TRADOVATE_CID=0
TRADOVATE_SECRET=
TRADOVATE_DEVICE_ID=tradovate-bot-vps

# Environment: demo or live
TRADOVATE_ENV=demo

# Prop Firm
PROP_FIRM=fundednext

# Logging
LOG_LEVEL=INFO
LOG_FILE=bot.log

# Remote Management API
MGMT_API_KEY=CHANGE_ME
MGMT_PORT=9090
ENVEOF
    chmod 600 "$ENV_FILE"
    log ".env template created."
else
    log ".env file already exists — keeping it."
fi

# Remove stale token (will re-authenticate fresh)
rm -f "$BOT_DIR/.tradovate_token.json"

# ── 5. Systemd service ─────────────────────────────────────
log "Installing systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Tradovate Trading Bot
After=network-online.target
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python bot.py --live
EnvironmentFile=$BOT_DIR/.env

# Auto-restart: bot sleeps outside market hours, exits at force close,
# systemd restarts it and it sleeps until next market open.
Restart=always
RestartSec=30

# Logging to journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

# Security
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$BOT_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" > /dev/null 2>&1
log "Systemd service installed and enabled."

# ── 5b. Management API service ────────────────────────────
MGMT_SERVICE="tradovate-mgmt"
log "Installing management API service..."
cat > "/etc/systemd/system/${MGMT_SERVICE}.service" << EOF
[Unit]
Description=Tradovate Bot Management API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python remote_api.py
EnvironmentFile=$BOT_DIR/.env

Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=$MGMT_SERVICE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$MGMT_SERVICE" > /dev/null 2>&1
log "Management API service installed."

# Generate a random API key if not already set
if grep -q "CHANGE_ME" "$ENV_FILE" 2>/dev/null; then
    RANDOM_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    sed -i "s/MGMT_API_KEY=CHANGE_ME/MGMT_API_KEY=$RANDOM_KEY/" "$ENV_FILE"
    log "Generated random MGMT_API_KEY: $RANDOM_KEY"
    warn "SAVE THIS KEY — you'll need it for remote_ctl.py"
fi

# ── 6. Log rotation ────────────────────────────────────────
cat > "/etc/logrotate.d/$SERVICE_NAME" << EOF
$BOT_DIR/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
EOF
log "Log rotation configured."

# ── 7. Helper script ───────────────────────────────────────
cat > "$BOT_DIR/bot-ctl.sh" << 'CTLEOF'
#!/bin/bash
# Quick control script for Tradovate Bot
SERVICE="tradovate-bot"

case "${1:-status}" in
    start)   sudo systemctl start "$SERVICE" && echo "Bot started." ;;
    stop)    sudo systemctl stop "$SERVICE" && echo "Bot stopped." ;;
    restart) sudo systemctl restart "$SERVICE" && echo "Bot restarted." ;;
    status)  sudo systemctl status "$SERVICE" --no-pager ;;
    logs)    sudo journalctl -u "$SERVICE" -f --no-pager ;;
    logs50)  sudo journalctl -u "$SERVICE" -n 50 --no-pager ;;
    update)
        echo "Pulling latest code..."
        cd /root/tradovate-bot
        sudo systemctl stop "$SERVICE"
        git pull origin "$(git branch --show-current)"
        source venv/bin/activate
        pip install -r requirements.txt -q
        deactivate
        sudo systemctl start "$SERVICE"
        echo "Updated and restarted."
        ;;
    *)
        echo "Usage: ./bot-ctl.sh {start|stop|restart|status|logs|logs50|update}"
        ;;
esac
CTLEOF
chmod +x "$BOT_DIR/bot-ctl.sh"

# ── 8. Summary ──────────────────────────────────────────────
echo ""
echo -e "${CYAN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${CYAN}════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Directory:  ${YELLOW}$BOT_DIR${NC}"
echo -e "  Config:     ${YELLOW}$BOT_DIR/.env${NC}"
echo -e "  Service:    ${YELLOW}$SERVICE_NAME${NC}"
echo ""
echo -e "  ${CYAN}Quick commands:${NC}"
echo ""
echo -e "    ${GREEN}./bot-ctl.sh start${NC}     Start the bot"
echo -e "    ${GREEN}./bot-ctl.sh stop${NC}      Stop the bot"
echo -e "    ${GREEN}./bot-ctl.sh restart${NC}   Restart the bot"
echo -e "    ${GREEN}./bot-ctl.sh status${NC}    Check status"
echo -e "    ${GREEN}./bot-ctl.sh logs${NC}      Watch live logs"
echo -e "    ${GREEN}./bot-ctl.sh update${NC}    Pull latest & restart"
echo ""
echo -e "  ${CYAN}Or use systemd directly:${NC}"
echo ""
echo -e "    ${GREEN}sudo systemctl start $SERVICE_NAME${NC}"
echo -e "    ${GREEN}sudo systemctl stop $SERVICE_NAME${NC}"
echo -e "    ${GREEN}sudo journalctl -u $SERVICE_NAME -f${NC}"
echo ""

if grep -q "your_password_here" "$ENV_FILE" 2>/dev/null; then
    echo -e "  ${RED}>>> IMPORTANT: Edit .env with your password first! <<<${NC}"
    echo -e "      ${YELLOW}nano $BOT_DIR/.env${NC}"
    echo ""
fi

echo -e "  ${CYAN}Remote Management API:${NC}"
echo ""
echo -e "    ${GREEN}sudo systemctl start $MGMT_SERVICE${NC}   Start mgmt API"
echo -e "    ${GREEN}sudo systemctl status $MGMT_SERVICE${NC}  Check mgmt API"
echo ""
echo -e "  ${CYAN}From Claude Code (after setting VPS_URL + MGMT_API_KEY in .env):${NC}"
echo ""
echo -e "    ${GREEN}python remote_ctl.py status${NC}    Full bot status"
echo -e "    ${GREEN}python remote_ctl.py start${NC}     Start the bot"
echo -e "    ${GREEN}python remote_ctl.py stop${NC}      Stop the bot"
echo -e "    ${GREEN}python remote_ctl.py logs${NC}      View recent logs"
echo ""
echo -e "  ${GREEN}When ready, start everything:${NC}"
echo -e "      ${YELLOW}cd $BOT_DIR${NC}"
echo -e "      ${YELLOW}sudo systemctl start $MGMT_SERVICE${NC}"
echo -e "      ${YELLOW}sudo systemctl start $SERVICE_NAME${NC}"
echo ""
