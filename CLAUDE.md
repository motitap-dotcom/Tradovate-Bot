# Tradovate Bot — Claude Code Guide

## CRITICAL: READ THIS FIRST

### How Moti manages the bot
**Moti manages this bot 100% through Claude Code. No terminal. No SSH.**
Everything — starting, stopping, monitoring, fixing, deploying — is done
from Claude Code via the GitHub bridge to the VPS.

**DO NOT** try to run `bot.py` locally in Claude Code — it will fail
(sandbox blocks tradovateapi.com). The bot runs ONLY on the VPS.

### Authentication: Playwright Browser ("Machzai")
**NEVER FORGET**: Auth uses **Playwright headless browser** ("machzai").
- FundedNext requires reCAPTCHA → only a real browser can handle it
- Playwright on the VPS logs into `https://trader.tradovate.com` automatically
- Captures the auth token and saves to `.tradovate_token.json`
- Token auto-renews every ~80 minutes
- If renewal fails → Playwright re-authenticates automatically
- **This is the ONLY auth method that works reliably for FundedNext**

### VPS Details
- **IP**: `77.237.234.2`
- **User**: `root`
- **SSH**: `ssh root@77.237.234.2`
- **Bot directory**: `/root/Tradovate-Bot`
- SSH is blocked from Claude Code sandbox. If SSH is needed, ask Moti to
  run commands on the VPS and paste the output.

---

## Operations Guide

### Architecture
The bot runs on a **VPS**. `server_agent.py` bridges Claude Code ↔ VPS:

```
Claude Code  ──push code/commands──►  GitHub  ◄──poll (30s)──  server_agent.py (VPS)
                                                                     │
Claude Code  ◄──read status.json───  GitHub  ◄──push (60s)──  bot.py (VPS)
```

### How to manage the bot (ALL from Claude Code)

| Action | How |
|--------|-----|
| **Check status** | `git pull` then read `github_control/status.json` |
| **Send command** | Write `command.json`, commit & push (agent polls every 30s) |
| **Deploy code** | Push code changes → agent auto-pulls & restarts bot |
| **View logs** | Read `status.json` → `recent_log` field |
| **Emergency** | Send `emergency_stop` command (closes all + stops) |

### Available commands (via command.json)
```json
{"command": "<cmd>", "id": "<unique-id>", "source": "claude",
 "timestamp": "<iso-timestamp>"}
```

| Command | What it does |
|---------|-------------|
| `start` | Start the bot if stopped |
| `stop` | Stop the bot gracefully |
| `restart` | Stop + start the bot |
| `deploy` | Pull latest code + restart |
| `status` | Force a status update |
| `emergency_stop` | Close all positions + cancel orders + stop bot |
| `close_all` | Close all open positions |
| `cancel_all` | Cancel all working orders |
| `refresh_token` | Renew the auth token |

### Status fields (github_control/status.json)
- `bot_running` / `bot_pid` — is the bot alive
- `token.status` / `token.minutes_remaining` — auth token health
- `account.balance` / `account.day_pnl` — account info
- `risk` — open contracts, trades today, locked status
- `recent_log` — last 20 log lines from the bot
- `journal_summary` — trade statistics

---

## Account Info
- **Prop Firm**: FundedNext (Futures Challenge)
- **Username**: FNFTMOTITAPWnBks
- **Account**: FNFTCHMOTITAPIRO67510 (id=39996695)
- **User ID**: 5644210
- **Environment**: demo API (but auth may go through live endpoint)
- **Starting Balance**: $50,000
- **Current Balance**: ~$52,426 (as of 2026-02-26)

## Authentication
Token is stored in `.tradovate_token.json` and auto-renewed.

**Auth flow** (in `tradovate_api.py:authenticate()`):
1. Saved token from `.tradovate_token.json` (renewed via API)
2. Web-style API auth (cid=8, no secret)
3. If demo auth fails → try live endpoint (FundedNext requires this)
4. API-key auth (CID + Secret)
5. **Playwright browser login** ("machzai") — handles CAPTCHA automatically

The bot auto-detects whether the account is on demo or live API and
switches `base_url` accordingly (fixed 2026-02-26).

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
python bot.py               # Main entry (uses TRADOVATE_ENV from .env, default: demo)
python bot.py --live        # Force live mode (DO NOT USE for FundedNext challenge)
python bot.py --dry-run     # Paper mode — signals only, no real orders
python browser_bot.py       # Browser-based auth then run bot
python get_token.py         # One-time token capture (needs display)
python check_status.py      # Quick health check report
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

## Fixes Log (2026-02-26)
- **`server_agent.py`**: Removed hardcoded `--live` flag (was sending orders to live API)
- **`server_agent.py`**: Added `git stash` before pull (fixes dirty working tree blocking pulls)
- **`bot.py`**: Token auto-renewal in main loop (every 30s)
- **`bot.py`**: Market price fallback for fill_price (fixes entry_price=0)
- **`bot.py`**: Removed warmup code that consumed ORB breakouts (was preventing mid-day trading)
- **`tradovate_api.py`**: Auto-detect demo vs live API for account operations
- **`risk_manager.py`**: SOD balance from API instead of config default (was incorrectly triggering profit cap)
- **12 ghost trades cleaned**: All from 2026-02-23, had entry_price=0

## Common Issues
1. **CAPTCHA required**: Bot auto-handles via Playwright ("machzai")
2. **"Account not found"**: Bot auto-detects correct API (demo vs live)
3. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth
4. **Claude Code can't reach Tradovate**: Sandbox proxy blocks it — manage via GitHub bridge only
5. **Git sync stuck**: `server_agent.py` now stashes before pull (fixed 2026-02-26)
6. **Wrong day PnL on startup**: SOD balance now initialized from API (fixed 2026-02-26)
