#!/bin/bash
# Setup script: opens port 8080 for the remote control API
# Run once on the server: bash setup_remote.sh

echo "Opening port 8080 for remote control API..."

# Try ufw first
if command -v ufw &>/dev/null; then
    ufw allow 8080/tcp
    echo "ufw: port 8080 opened"
fi

# Try iptables as fallback
if command -v iptables &>/dev/null; then
    iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
    echo "iptables: port 8080 opened"
fi

echo ""
echo "Remote control API will be available at:"
echo "  http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR-SERVER-IP'):8080/api/summary"
echo ""
echo "Restarting bot to activate remote control..."
systemctl restart tradovate-bot
echo "Done!"
