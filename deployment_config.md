# Deployment Configuration

## Server Details (backup reference)
- **Server IP**: 77.237.234.2
- **SSH Username**: root
- **Repo Path**: /root/tradovate-bot

## GitHub Secrets Required

Go to: **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Value | Description |
|-------------|-------|-------------|
| `VPS_HOST` | `77.237.234.2` | Server IP address |
| `VPS_USER` | `root` | SSH username |
| `VPS_SSH_KEY` | (private key) | SSH private key (full, including BEGIN/END lines) |
| `VPS_PORT` | `22` | SSH port |
| `SERVER_GH_PAT` | (GitHub PAT) | GitHub Personal Access Token for status pushes |
| `TRADOVATE_USERNAME` | (username) | Tradovate login (for status checks) |
| `TRADOVATE_PASSWORD` | (password) | Tradovate password (for status checks) |
| `TRADOVATE_ACCESS_TOKEN` | (optional) | Cached token to skip CAPTCHA |

### SSH Key Notes
The `VPS_SSH_KEY` must:
- Start with: `-----BEGIN OPENSSH PRIVATE KEY-----`
- End with: `-----END OPENSSH PRIVATE KEY-----`
- Include both lines above (don't skip them!)
- Be copied in full, no extra spaces at start/end

## Deployment
Push to `main` is the only way to deploy. The server webhook automatically pulls changes.
