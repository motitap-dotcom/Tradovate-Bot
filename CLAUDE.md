# Tradovate Bot — Claude Code Guide

## !!! MANDATORY FIRST STEP — EVERY SESSION !!!

**Before doing ANYTHING else in any session, you MUST:**
1. **Read this entire CLAUDE.md file first** — it contains critical rules, architecture, and context
2. **Read `config.py`** — current trading settings, enabled contracts, risk parameters
3. **Read `bot.py`** — main orchestrator logic, understand the current flow
4. **Run `python bot_cmd.py read`** — check latest bot status from VPS

**Do NOT skip these steps. Do NOT start coding, debugging, or answering questions before completing them.**
**This ensures you always have full context and don't break anything.**

---

## !!! REMOTE CONTROL — READ THIS FIRST !!!

**The bot runs on a VPS. You control it from here via GitHub.**
**DO NOT modify `bot_cmd.py`, `vps_agent.py`, `.bot_command.json`, `.bot_status.json`.**
**These files are the communication channel — changing them will break remote control.**

### Quick Commands (use these immediately in any session):

```bash
python bot_cmd.py read           # Read latest bot status (instant, no wait)
python bot_cmd.py status         # Request fresh status from VPS (~15-30s)
python bot_cmd.py start          # Start the bot
python bot_cmd.py stop           # Stop the bot
python bot_cmd.py restart        # Restart the bot
python bot_cmd.py logs           # View recent log lines
python bot_cmd.py activity       # Recent signals, trades, locks
python bot_cmd.py token          # Auth token status
python bot_cmd.py update         # Git pull + restart on VPS
python bot_cmd.py ping           # Health check
```

### How It Works (DO NOT CHANGE THIS MECHANISM):

1. `bot_cmd.py` writes a command to `.bot_command.json` and pushes to GitHub
2. `vps_agent.py` on the VPS pulls from GitHub every 15 seconds
3. VPS agent executes the command (start/stop/status/etc.)
4. VPS agent writes result to `.bot_status.json` and pushes back to GitHub
5. `bot_cmd.py` fetches the result via `git fetch`

**Response time**: 15-30 seconds (depends on VPS poll cycle)
**Auto status**: VPS pushes status every 5 minutes even without commands

### VPS Info

- **IP**: `77.237.234.2`
- **User**: `root`
- **Password**: `Moti0417!`
- **Bot directory**: `/root/Tradovate-Bot`
- **Services**: `tradovate-bot` (trading), `tradovate-agent` (GitHub poller)

### If VPS Agent Is Down (emergency only):

The user must SSH from their terminal:
```bash
ssh root@77.237.234.2
systemctl start tradovate-agent   # Start the GitHub poller
systemctl start tradovate-bot     # Start the trading bot
```

---

## Account Info
- **Prop Firm**: FundedNext (Futures Challenge)
- **Username**: FNFTMOTITAPWnBks
- **Account**: FNFTCHMOTITAPIRO67510 (Demo, id=39996695)
- **User ID**: 5644210
- **Organization**: FundedNext (id=44)
- **Environment**: demo (challenge phase uses demo API)
- **Starting Balance**: $50,000
- **Current Balance**: ~$52,426 (as of 2026-02-24)

## Architecture

```
bot.py                  — Main orchestrator: lifecycle, market data, order execution
├── tradovate_api.py    — REST + WebSocket client (auth, orders, positions, market data)
├── strategies.py       — Signal generation: ORB (indices), VWAP (commodities)
├── risk_manager.py     — Drawdown enforcement, position sizing, daily loss limits
├── config.py           — All settings, loaded from .env
├── vps_agent.py        — GitHub poller: reads commands, pushes results (DO NOT MODIFY)
├── bot_cmd.py          — Command sender: pushes commands via git (DO NOT MODIFY)
├── .bot_command.json   — Command file: Claude Code -> VPS (DO NOT MODIFY)
├── .bot_status.json    — Status file: VPS -> Claude Code (DO NOT MODIFY)
├── remote_api.py       — HTTP management API (backup, port 9090)
├── remote_ctl.py       — HTTP remote control client (backup)
├── browser_bot.py      — Alternative entry: browser-based auth + bot
└── get_token.py        — One-time token capture (requires display)
```

## Trading Rules (FundedNext Challenge)
- Max trailing drawdown: $2,500
- Daily loss limit: $1,000
- Profit target: $3,000
- Max contracts: 10 (minis)
- Close by: 4:59 PM ET
- Drawdown trails unrealized intraday peaks

## Enabled Contracts
- **NQ** (E-mini Nasdaq): ORB strategy, 25pt stop / 50pt TP
- **ES** (E-mini S&P): ORB strategy, 6pt stop / 12pt TP
- **GC** (Gold): VWAP strategy, 5pt stop / 10pt TP
- **CL** (Crude Oil): VWAP strategy, 0.20pt stop / 0.40pt TP

## Authentication
Token is stored in `.tradovate_token.json` and auto-renewed every 15 min in the main loop.

**Auth flow priority** (in `tradovate_api.py:authenticate()`):
1. `TRADOVATE_ACCESS_TOKEN` env var (manual override)
2. Pre-injected token via `set_token()`
3. Saved token from `.tradovate_token.json` (renewed via API)
4. Web-style API auth (cid=8, no secret)
5. API-key auth (CID + Secret)
6. Playwright browser login (handles CAPTCHA automatically)

## API Notes
- FundedNext accounts use `organization: ""` (empty string, NOT "funded-next")
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket uses custom text protocol: `endpoint\nid\n{json}`
- Token expires ~80 minutes, auto-renewed via `/auth/renewaccesstoken`

## Testing
```bash
python -m pytest test_all.py -v
```

## Common Issues
1. **"Incorrect password"**: Credentials are correct; try `live` API (not `demo` for auth)
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login
3. **Empty account list**: FundedNext challenge accounts are on demo API
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth
5. **bot_cmd.py timeout**: VPS agent may be down — user needs to SSH and start `tradovate-agent`
