#!/bin/bash
# Send email alert when Tradovate bot stops
# Used by systemd ExecStopPost

SERVICE="$1"
STATUS=$(systemctl is-failed "$SERVICE" 2>/dev/null)
HOSTNAME=$(hostname)
DATE=$(date '+%Y-%m-%d %H:%M:%S UTC')
EMAIL="motitap@gmail.com"

SUBJECT="[ALERT] Tradovate Bot stopped on $HOSTNAME"
BODY="Service: $SERVICE
Status: $STATUS
Time: $DATE
Host: $HOSTNAME

The Tradovate trading bot has stopped unexpectedly.
systemd will attempt to restart it automatically.

Check logs: journalctl -u $SERVICE -n 50"

echo "$BODY" | mail -s "$SUBJECT" "$EMAIL" 2>/dev/null || \
echo "$BODY" | sendmail "$EMAIL" 2>/dev/null || \
echo "[$(date)] Alert: Could not send email - mail/sendmail not installed" >> /root/tradovate-bot/bot.log
