# Tradovate Bot — Claude Code Guide

## IMPORTANT: Operations Guide (READ FIRST)

**The user (Moti) manages this bot exclusively through Claude Code.**
Claude Code is responsible for: starting/stopping the bot, monitoring trades,
fixing bugs, deploying code changes, and checking status.

### How the bot runs
- The bot runs **in this Claude Code environment** as a background process
- Start: `nohup python3 bot.py > bot.log 2>&1 & echo $! > bot.pid`
- Stop: `kill $(cat bot.pid)`
- Status: `python3 check_status.py` (or `--watch` for live monitoring)
- Logs: `tail -50 bot.log`

### Known environment limitation: Proxy blocks Tradovate API
This Claude Code sandbox has a proxy that **blocks direct access** to
`tradovateapi.com` and `trader.tradovate.com`. The bot CANNOT authenticate
on its own from this environment. A valid token must be provided manually.

### How to get the bot running (every time token expires)
1. Ask Moti to log in at https://trader.tradovate.com in his browser
2. He opens DevTools (F12) → Network tab → finds the auth response
3. He pastes the `accessToken` value here
4. Claude saves it to `.tradovate_token.json` or `.env` as `TRADOVATE_ACCESS_TOKEN`
5. Claude starts the bot: `nohup python3 bot.py > bot.log 2>&1 &`
6. The bot auto-renews the token every ~75 minutes (before 80min expiry)
7. **As long as the bot keeps running, the token stays alive**
8. If the bot crashes or the session restarts, we need a new token from Moti

### What was fixed (2026-02-26)
- **`server_agent.py`**: Removed hardcoded `--live` flag. Bot now uses
  `TRADOVATE_ENV=demo` from `.env` (FundedNext challenge is on demo API)
- **`bot.py`**: Added token auto-renewal in main loop (every 30s)
- **`bot.py`**: Added market price fallback for fill_price (fixes entry_price=0)
- **12 ghost trades in journal**: All from 2026-02-23, had entry_price=0
  because orders went to live API (wrong endpoint). Cleaned up as stale.

### Quick reference
| Action | Command |
|--------|---------|
| Start bot | `nohup python3 bot.py > bot.log 2>&1 & echo $! > bot.pid` |
| Stop bot | `kill $(cat bot.pid)` |
| Check status | `python3 check_status.py` |
| Live monitor | `python3 check_status.py --watch` |
| View log | `tail -50 bot.log` |
| Run tests | `python -m pytest test_all.py -v` |

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

## Common Issues
1. **"Incorrect password"**: Credentials are correct; try `live` API (not `demo` for auth)
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login
3. **Empty account list**: FundedNext challenge accounts are on demo API
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth
5. **ProxyError in Claude Code env**: This sandbox blocks tradovateapi.com — need manual token from Moti
6. **entry_price=0 in journal**: Orders went to wrong API endpoint (live vs demo) — fixed 2026-02-26
7. **Token expired / bot stopped**: Ask Moti for a new token from browser DevTools
