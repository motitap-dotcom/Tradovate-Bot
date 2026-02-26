#!/bin/bash
# ============================================================
# One-Command Server Setup
# ============================================================
# Run this ONCE on your VPS to set up everything:
#   bash setup_agent.sh
#
# What it does:
#   1. Installs dependencies (Playwright, etc.)
#   2. Opens port 8080 for local remote control API
#   3. Sets up the server agent as a systemd service
#   4. Starts the agent (which auto-starts the bot)
# ============================================================

set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

echo "============================================================"
echo "  Tradovate Bot — Server Setup"
echo "============================================================"
echo "  Bot directory: $BOT_DIR"
echo ""

# ─────────────────────────────────────────
# 1. Install Python dependencies
# ─────────────────────────────────────────
echo "[1/5] Installing Python dependencies..."
pip install -q requests python-dotenv 2>/dev/null || true

# Try to install Playwright (optional, for CAPTCHA bypass)
echo "[2/5] Installing Playwright (for CAPTCHA bypass)..."
pip install -q playwright 2>/dev/null || true
playwright install chromium 2>/dev/null || echo "  ⚠ Playwright browser install failed (may need manual install)"
playwright install-deps chromium 2>/dev/null || true

# ─────────────────────────────────────────
# 2. Open firewall port
# ─────────────────────────────────────────
echo "[3/5] Opening port 8080..."
if command -v ufw &>/dev/null; then
    ufw allow 8080/tcp 2>/dev/null || true
fi
if command -v iptables &>/dev/null; then
    iptables -I INPUT -p tcp --dport 8080 -j ACCEPT 2>/dev/null || true
fi

# ─────────────────────────────────────────
# 3. Create systemd service for the agent
# ─────────────────────────────────────────
echo "[4/5] Creating systemd service..."

if command -v systemctl &>/dev/null && [ -d /etc/systemd/system ]; then
    cat > /etc/systemd/system/tradovate-agent.service << SERVICEEOF
[Unit]
Description=Tradovate Bot Server Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
ExecStart=$(which python3 || which python) $BOT_DIR/server_agent.py --interval 30
Restart=always
RestartSec=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-$BOT_DIR/.env

[Install]
WantedBy=multi-user.target
SERVICEEOF

    systemctl daemon-reload
    systemctl enable tradovate-agent
    systemctl restart tradovate-agent
    echo "  ✓ Agent service created and started"
    echo ""
    echo "  Useful commands:"
    echo "    systemctl status tradovate-agent    # Check agent status"
    echo "    journalctl -u tradovate-agent -f    # Follow agent logs"
    echo "    systemctl restart tradovate-agent   # Restart agent"
else
    echo "  ⚠ systemd not available — starting agent in background"
    # Kill existing agent if running
    pkill -f "python.*server_agent" 2>/dev/null || true
    sleep 1
    nohup python3 "$BOT_DIR/server_agent.py" --interval 30 > "$BOT_DIR/agent.log" 2>&1 &
    echo "  ✓ Agent started (PID: $!)"
    echo "  ⚠ Note: agent won't auto-restart on reboot without systemd"
    echo "  Add to crontab: @reboot cd $BOT_DIR && python3 server_agent.py &"
fi

# ─────────────────────────────────────────
# 4. Also create/update bot service
# ─────────────────────────────────────────
if command -v systemctl &>/dev/null && [ -d /etc/systemd/system ]; then
    cat > /etc/systemd/system/tradovate-bot.service << BOTEOF
[Unit]
Description=Tradovate Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
ExecStart=$(which python3 || which python) $BOT_DIR/bot.py --live
Restart=on-failure
RestartSec=30
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-$BOT_DIR/.env

[Install]
WantedBy=multi-user.target
BOTEOF
    systemctl daemon-reload
fi

# ─────────────────────────────────────────
# 5. Git configuration
# ─────────────────────────────────────────
echo "[5/5] Configuring git..."
cd "$BOT_DIR"
git config pull.rebase false 2>/dev/null || true

echo ""
echo "============================================================"
echo "  ✓ Setup complete!"
echo "============================================================"
echo ""
echo "  The server agent is now running and will:"
echo "    • Poll GitHub every 30 seconds for commands"
echo "    • Auto-start the bot"
echo "    • Push status updates to GitHub"
echo "    • Auto-deploy code changes"
echo ""
echo "  Status: http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR-IP'):8080/api/summary"
echo ""
echo "  From Claude Code, you can now manage everything remotely!"
echo "============================================================"
