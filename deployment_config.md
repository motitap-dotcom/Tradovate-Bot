# Deployment Configuration

## Server Details
- **Server IP**: 77.237.234.2
- **SSH Username**: root
- **Repo Path**: /root/Tradovate-Bot
- **Deployment Method**: CI/CD via Webhook (Port 8000)
- **Update Command**: `git pull origin main`

## Diagnostics

Health check commands to verify the server and bot are running correctly:

| Check | Command | Expected |
|-------|---------|----------|
| **Check Server** | `ping -c 3 77.237.234.2` | 3 packets received, 0% loss |
| **Check Webhook** | `curl -I http://77.237.234.2:8000` | HTTP 200 response |
| **Check Bot Process** | `ssh root@77.237.234.2 'pm2 status'` | Bot process showing `online` |

If `pm2` is not installed, use the fallback command:
```bash
ssh root@77.237.234.2 'ps aux | grep bot.py | grep -v grep'
```

### Troubleshooting
- **Server not responding (ping fails)**: Check Contabo dashboard for VPS status, reboot if needed.
- **Webhook not responding (curl fails)**: SSH into the server and restart the webhook listener: `pm2 restart webhook` or manually re-run the listener script.
- **Bot not running**: SSH into the server and restart: `pm2 restart bot` or `cd /root/Tradovate-Bot && python bot.py --live &`.
