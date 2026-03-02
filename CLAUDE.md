# Tradovate Bot — Claude Code Guide

## Fixed Rules (ALWAYS follow these)

1. **Language**: ALWAYS communicate in Hebrew (עברית). Every response, comment, and explanation must be in Hebrew.
2. **Server Communication**: NEVER use SSH directly. ALL server communication is done via GitHub Actions (Push & Listen). See "Server Communication" section below.
3. **User**: The user's name is **Noa (נועה)**. She is NOT a developer. Always give simple, clear instructions. Avoid technical jargon when possible.
4. **Local Machine**: Windows with PowerShell.
5. **VPS**: Contabo, IP `77.237.234.2`, Ubuntu Linux.
6. **Telegram Bot**: Token `8452836462:AAEVGDT5JrxOHAcB8Nd8ayObU1iMQUCRk2g`, Chat ID `7013213983`.

---

## Server Communication

> **CRITICAL: NEVER use SSH to connect to the server. NEVER.**
>
> The ONLY way to communicate with the VPS is through **GitHub Actions (Push & Listen)**:
> 1. Make code changes locally
> 2. Commit and push to `main`
> 3. The server webhook automatically pulls changes and restarts the bot
>
> Do NOT attempt to run diagnostic commands (ping, curl, ssh) — they are irrelevant from this environment.

---

## Project Details — Tradovate Trading Bot

### Account Info
- **Prop Firm**: FundedNext (Futures Challenge)
- **Username**: FNFTMOTITAPWnBks
- **Account**: FNFTCHMOTITAPIRO67510 (Demo, id=39996695)
- **User ID**: 5644210
- **Organization**: FundedNext (id=44)
- **Environment**: demo (challenge phase uses demo API)
- **Starting Balance**: $50,000

### Trading Rules (FundedNext Challenge)
- Max trailing drawdown: $2,500
- Daily loss limit: $1,000
- Profit target: $3,000
- Max contracts: 10 (minis)
- Close by: 4:59 PM ET
- Drawdown trails unrealized intraday peaks

### Enabled Contracts
- **NQ** (E-mini Nasdaq): ORB strategy, 25pt stop / 50pt TP
- **ES** (E-mini S&P): ORB strategy, 6pt stop / 12pt TP
- **GC** (Gold): VWAP strategy, 5pt stop / 10pt TP
- **CL** (Crude Oil): VWAP strategy, 0.20pt stop / 0.40pt TP

### Architecture

```
bot.py                  — Main orchestrator: lifecycle, market data, order execution
├── tradovate_api.py    — REST + WebSocket client (auth, orders, positions, market data)
├── strategies.py       — Signal generation: ORB (indices), VWAP (commodities)
├── risk_manager.py     — Drawdown enforcement, position sizing, daily loss limits
├── config.py           — All settings, loaded from .env
├── browser_bot.py      — Alternative entry point: browser-based auth + bot
└── get_token.py        — One-time token capture (requires display)
```

### Running

```bash
python bot.py --live        # Main entry (uses .env TRADOVATE_ENV)
python browser_bot.py       # Browser-based auth then run bot
python get_token.py         # One-time token capture (needs display)
```

### Key Files

| File | Purpose |
|------|---------|
| `.env` | Credentials and config (never commit) |
| `.tradovate_token.json` | Cached auth token (auto-renewed) |
| `config.py` | Contract specs, challenge limits, trading hours |
| `tradovate_api.py` | Full Tradovate API client |
| `strategies.py` | ORB + VWAP strategies |
| `risk_manager.py` | Position sizing + drawdown protection |
| `test_all.py` | Comprehensive test suite |

### Authentication
Token is stored in `.tradovate_token.json` and auto-renewed.

**Auth flow priority** (in `tradovate_api.py:authenticate()`):
1. `TRADOVATE_ACCESS_TOKEN` env var (manual override)
2. Pre-injected token via `set_token()`
3. Saved token from `.tradovate_token.json` (renewed via API)
4. Web-style API auth (cid=8, no secret)
5. API-key auth (CID + Secret)
6. Playwright browser login (handles CAPTCHA automatically)

**CAPTCHA handling**: FundedNext accounts on Tradovate require reCAPTCHA on first login from a new device. The bot uses Playwright headless browser to bypass this by logging in through the actual Tradovate web trader page.

### API Notes
- FundedNext accounts use `organization: ""` (empty string, NOT "funded-next")
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket uses custom text protocol: `endpoint\nid\n{json}`
- Token expires ~80 minutes, auto-renewed via `/auth/renewaccesstoken`
- reCAPTCHA sitekey: `6Ld7FAoTAAAAAPdydZWpQ__C8xf29eYfvswcz52T`

---

## Current Status

- **Balance**: ~$52,426 (as of 2026-02-24)
- **SOD Balance**: ~$48,094 (as of 2026-02-23)
- **Bot Status**: Active on VPS
- **Last Updated**: 2026-03-02

---

## Testing

```bash
python -m pytest test_all.py -v
```

---

## Development Workflow

Push to `main` is the only way to deploy. The server webhook automatically pulls changes.

### Task Completion Procedure
Every time you finish writing code, fixing a bug, or making a change requested by the user: push to `main` and report: **"הקוד מוכן, ביצעתי Push ל-main כדי שהשרת יתעדכן."** Do NOT run diagnostic tests (ping/curl/ssh) — they are irrelevant from this environment.

---

## How to Resume Work

If you are a new Claude session picking up this project, follow these steps:

1. **Read this file first** — it contains all the rules, project details, and current status.
2. **Language**: Speak Hebrew. The user is Noa (נועה), not a developer.
3. **Never SSH** — all server updates go through Git push to `main`.
4. **Check current status** section above for the latest balance and bot state.
5. **Check recent git history** (`git log --oneline -10`) to understand what was done recently.
6. **Check `.env`** for current configuration (but never commit it).
7. **Ask Noa** if anything is unclear — give her simple options, not technical questions.

---

## Common Issues
1. **"Incorrect password"**: Credentials are correct; try `live` API (not `demo` for auth)
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login
3. **Empty account list**: FundedNext challenge accounts are on demo API
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth
