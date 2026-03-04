# Tradovate Bot — Claude Code Guide

## ⚠️ Server Management — Workflow Only (MANDATORY)

**אין גישה ישירה לשרת. כל פעולה על השרת מתבצעת אך ורק דרך זרימת עבודה (GitHub Actions + Push).**

This is the **#1 rule** for this project. Claude Code does NOT have SSH access, cannot ping the server, cannot run curl to the server, and cannot execute any remote commands directly. The ONLY way to manage the bot on its production server is through the repository workflow:

### How It Works (Push → Deploy → Listen)

```
1. PUSH   — Make code changes and push to `main` branch
2. DEPLOY — GitHub Actions (deploy.yml) SSHs to server, pulls code, restarts service
3. LISTEN — Server cron (server_cron.sh) pushes status back to GitHub every 5 min
```

### What You CAN Do
- **Deploy changes**: Edit code → commit → push to `main` → server auto-updates
- **Check server status**: Read `server_status.json` (updated every 5 min by server cron)
- **Check system status**: Read `system_status.json` (updated every 30 min by GitHub Actions)
- **Trigger manual checks**: Push to main triggers `system-status.yml` and `server-health-check.yml`
- **Modify bot behavior**: Change code files → push → server restarts with new code
- **Start/stop bot**: Modify the service configuration in deploy workflow → push

### What You MUST NOT Do
- ❌ Do NOT try to SSH to the server
- ❌ Do NOT run ping, curl, or any network diagnostic to the server
- ❌ Do NOT attempt direct API calls to the server
- ❌ Do NOT try to read server logs directly — they come through `server_status.json`
- ❌ Do NOT suggest the user needs to manually do anything on the server

### Workflow Files Reference
| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `deploy.yml` | Push to main | Deploy code to server via SSH |
| `auto-merge-deploy.yml` | Push to claude/** | Auto-merge to main + deploy |
| `ci.yml` | Push/PR | Run tests and syntax checks |
| `server-health-check.yml` | Every 15 min | Read server_status.json |
| `system-status.yml` | Every 30 min | Full system check via SSH |
| `connectivity-test.yml` | Every 6 hours | Test Tradovate API endpoints |

### Task Completion Protocol
בכל פעם שסיימת לכתוב קוד, לתקן באג או לבצע שינוי:
1. **Commit** the changes with a clear message
2. **Push to `main`** (or merge from feature branch)
3. **Report**: "הקוד מוכן, ביצעתי Push ל-main כדי שהשרת יתעדכן."
4. **Check status**: Read `server_status.json` after ~5 minutes to confirm deployment

**זה חל על כל חלון חדש, כל שיחה חדשה, וכל בקשה. אין חריגים.**

---

## Account Info
- **Prop Firm**: FundedNext (Futures Challenge)
- **Username**: FNFTMOTITAPWnBks
- **Account**: FNFTCHMOTITAPIRO67510 (Demo, id=39996695)
- **User ID**: 5644210
- **Organization**: FundedNext (id=44)
- **Environment**: demo (challenge phase uses demo API)
- **Starting Balance**: $50,000
- **SOD Balance**: ~$48,094 (as of 2026-02-23)

## Authentication
Token is stored in `.tradovate_token.json` and auto-renewed.

**Auth flow priority** (in `tradovate_api.py:authenticate()`):
1. `TRADOVATE_ACCESS_TOKEN` env var (manual override)
2. Pre-injected token via `set_token()`
3. Saved token from `.tradovate_token.json` (renewed via API)
4. Web-style API auth (cid=8, no secret)
5. API-key auth (CID + Secret)
6. Playwright browser login (handles CAPTCHA automatically)

**CAPTCHA handling**: FundedNext accounts on Tradovate require reCAPTCHA on
first login from a new device. The bot uses Playwright headless browser to
bypass this by logging in through the actual Tradovate web trader page.
The browser needs the HTTPS_PROXY env var configured (auto-detected).

## Architecture

```
bot.py                  — Main orchestrator: lifecycle, market data, order execution
├── tradovate_api.py    — REST + WebSocket client (auth, orders, positions, market data)
├── strategies.py       — Signal generation: ORB (indices), VWAP (commodities)
├── risk_manager.py     — Drawdown enforcement, position sizing, daily loss limits
├── config.py           — All settings, loaded from .env
├── browser_bot.py      — Alternative entry point: browser-based auth + bot
└── get_token.py        — One-time token capture (requires display)
```

## Running

```bash
python bot.py --live        # Main entry (uses .env TRADOVATE_ENV)
python browser_bot.py       # Browser-based auth then run bot
python get_token.py         # One-time token capture (needs display)
```

## Key Files

| File | Purpose |
|------|---------|
| `.env` | Credentials and config (never commit) |
| `.tradovate_token.json` | Cached auth token (auto-renewed) |
| `config.py` | Contract specs, challenge limits, trading hours |
| `tradovate_api.py` | Full Tradovate API client |
| `strategies.py` | ORB + VWAP strategies |
| `risk_manager.py` | Position sizing + drawdown protection |
| `test_all.py` | Comprehensive test suite |

## Trading Rules (FundedNext Challenge)
- Max trailing drawdown: $2,500
- Daily loss limit: $1,000
- Profit target: $3,000
- Max contracts: 10 (minis)
- Close by: 4:59 PM ET
- Drawdown trails unrealized intraday peaks
- Current balance: ~$52,426 (as of 2026-02-24)

## Enabled Contracts
- **NQ** (E-mini Nasdaq): ORB strategy, 25pt stop / 50pt TP
- **ES** (E-mini S&P): ORB strategy, 6pt stop / 12pt TP
- **GC** (Gold): VWAP strategy, 5pt stop / 10pt TP
- **CL** (Crude Oil): VWAP strategy, 0.20pt stop / 0.40pt TP

## API Notes
- FundedNext accounts use `organization: ""` (empty string, NOT "funded-next")
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket uses custom text protocol: `endpoint\nid\n{json}`
- Token expires ~80 minutes, auto-renewed via `/auth/renewaccesstoken`
- reCAPTCHA sitekey: `6Ld7FAoTAAAAAPdydZWpQ__C8xf29eYfvswcz52T`

## Testing
```bash
python -m pytest test_all.py -v
```

## Development Workflow
See **"Server Management — Workflow Only"** section at the top of this file.
All deployment and server management is done exclusively through GitHub workflows (push → deploy → listen).

## Common Issues
1. **"Incorrect password"**: Credentials are correct; try `live` API (not `demo` for auth)
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login
3. **Empty account list**: FundedNext challenge accounts are on demo API
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth
