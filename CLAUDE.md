# Tradovate Bot — Claude Code Guide

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
├── get_token.py        — One-time token capture (requires display)
├── remote_api.py       — HTTP management API (runs on VPS, port 9090)
└── remote_ctl.py       — Remote control client (used from Claude Code)
```

## Running

```bash
python bot.py --live        # Main entry (uses .env TRADOVATE_ENV)
python browser_bot.py       # Browser-based auth then run bot
python get_token.py         # One-time token capture (needs display)
```

## Remote Management (from Claude Code via GitHub)

The bot is managed remotely using GitHub as a communication channel.
The VPS runs `vps_agent.py` which polls GitHub every 15 seconds for commands.

**VPS**: `77.237.234.2` (root, password in session history)

```bash
# Send commands to the VPS via GitHub:
python bot_cmd.py status         # Full bot status
python bot_cmd.py start          # Start the bot
python bot_cmd.py stop           # Stop the bot
python bot_cmd.py restart        # Restart the bot
python bot_cmd.py logs           # View recent logs
python bot_cmd.py activity       # Recent signals/trades/locks
python bot_cmd.py token          # Token status
python bot_cmd.py update         # Git pull + restart on VPS
python bot_cmd.py ping           # Health check
python bot_cmd.py read           # Read latest status (no command sent)
```

How it works:
1. `bot_cmd.py` writes command to `.bot_command.json` and pushes to GitHub
2. `vps_agent.py` on VPS pulls every 15s, executes, writes result to `.bot_status.json`
3. `bot_cmd.py` fetches the result from GitHub

VPS services: `tradovate-bot` (the bot), `tradovate-agent` (GitHub poller)

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
| `remote_api.py` | Management API server (VPS side) |
| `remote_ctl.py` | Remote control client (Claude Code side) |
| `tradovate-mgmt.service` | Systemd service for mgmt API |

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

## Common Issues
1. **"Incorrect password"**: Credentials are correct; try `live` API (not `demo` for auth)
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login
3. **Empty account list**: FundedNext challenge accounts are on demo API
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth
