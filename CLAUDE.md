# Tradovate Bot — Claude Code Guide

## VPS Remote Server (IMPORTANT — Read First)

The bot runs on a remote VPS. Every Claude Code session MUST connect to the server to manage the bot.

### Connection Details

```
Host: 77.237.234.2
User: root
Port: 22
Command: ssh root@77.237.234.2
```

### How to Connect

1. On every new session, first test connectivity:
   ```bash
   ssh -o ConnectTimeout=10 root@77.237.234.2 "hostname && uptime"
   ```
2. If SSH key is missing, generate and copy:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/tradovate_vps -N ""
   ssh-copy-id -i ~/.ssh/tradovate_vps root@77.237.234.2
   ```
3. For convenience, add SSH config:
   ```bash
   mkdir -p ~/.ssh && cat >> ~/.ssh/config << 'EOF'
   Host tradovate-vps
       HostName 77.237.234.2
       User root
       Port 22
       IdentityFile ~/.ssh/tradovate_vps
   EOF
   ```

### Bot Management via SSH

```bash
# Check bot status
ssh root@77.237.234.2 "systemctl status tradovate-bot"

# View live logs
ssh root@77.237.234.2 "journalctl -u tradovate-bot -f --no-pager -n 50"

# Restart bot
ssh root@77.237.234.2 "systemctl restart tradovate-bot"

# Stop bot
ssh root@77.237.234.2 "systemctl stop tradovate-bot"

# Start bot
ssh root@77.237.234.2 "systemctl start tradovate-bot"

# Run bot manually (foreground, for debugging)
ssh root@77.237.234.2 "cd /opt/tradovate-bot && source venv/bin/activate && python bot.py"

# Run with Playwright (browser auth)
ssh root@77.237.234.2 "cd /opt/tradovate-bot && source venv/bin/activate && python browser_bot.py --headless"

# Deploy latest code from git
ssh root@77.237.234.2 "cd /opt/tradovate-bot && git pull && systemctl restart tradovate-bot"

# Check dashboard
ssh root@77.237.234.2 "systemctl status tradovate-dashboard"

# View trade journal
ssh root@77.237.234.2 "cd /opt/tradovate-bot && source venv/bin/activate && python trade_journal.py --today"

# View bot config
ssh root@77.237.234.2 "cat /opt/tradovate-bot/.env"
```

### Deploying Code Changes

After making changes locally, push to git and deploy:
```bash
git push origin <branch>
ssh root@77.237.234.2 "cd /opt/tradovate-bot && git pull origin main && systemctl restart tradovate-bot"
```

### VPS Paths

- **Bot directory**: `/opt/tradovate-bot/`
- **Virtual env**: `/opt/tradovate-bot/venv/`
- **Logs**: `/opt/tradovate-bot/bot.log`
- **Journal**: `/opt/tradovate-bot/trade_journal.json`
- **Token**: `/opt/tradovate-bot/.tradovate_token.json`
- **Tuner log**: `/opt/tradovate-bot/tuner_log.json`
- **systemd**: `/etc/systemd/system/tradovate-bot.service`

## Overview

Automated futures trading bot for the Tradovate platform, built for prop firm challenge accounts (FundedNext, Apex, Topstep). Executes ORB (Opening Range Breakout) and VWAP Momentum strategies with full risk management, auto-tuning, trade journaling, and a live web dashboard.

## Account Info

- **Prop Firm**: FundedNext (Futures Challenge)
- **Username**: FNFTMOTITAPWnBks
- **Account**: FNFTCHMOTITAPIRO67510 (Demo, id=39996695)
- **User ID**: 5644210
- **Organization**: FundedNext (id=44)
- **Environment**: demo (challenge phase uses demo API)
- **Starting Balance**: $50,000

## Architecture

```
bot.py                    — Main orchestrator: lifecycle, market data, signal execution
├── tradovate_api.py      — REST + WebSocket client (auth, orders, positions, market data)
├── strategies.py         — Signal generation: ORB (indices), VWAP (commodities)
├── risk_manager.py       — Drawdown enforcement, position sizing, daily loss/profit limits
├── config.py             — All settings, loaded from .env
├── trade_journal.py      — Trade recording, analytics, performance reports
├── auto_tuner.py         — End-of-day parameter adjustment (stops, targets, trade caps)
├── dashboard.py          — Flask web dashboard (real-time monitoring, port 8080)
├── publish_dashboard.py  — Static HTML dashboard generator for GitHub Pages
├── status.py             — Rich terminal status dashboard
├── browser_bot.py        — Alternative entry point: Playwright browser-based auth + bot
└── get_token.py          — One-time token capture via browser (requires display)
```

### Data Flow

1. **Auth**: `tradovate_api.py` authenticates (token file → web auth → API auth → browser login)
2. **Contract Resolution**: `bot.py` uses `/contract/suggest` to find front-month contracts
3. **Strategy Warmup**: Yahoo Finance 1-min candles feed into strategies to build ORB ranges / VWAP
4. **Market Data**: WebSocket (`MarketDataStream`) or REST polling (`RestMarketDataPoller` via Yahoo Finance)
5. **Signal Generation**: `strategies.py` produces `TradeSignal` on breakouts / VWAP crossovers
6. **Risk Check**: `risk_manager.py` validates position size, drawdown, daily limits
7. **Execution**: `tradovate_api.py` places bracket orders (`placeorder` + `placeOCO`)
8. **Journaling**: `trade_journal.py` records entries/exits with R-multiples
9. **Auto-Tuning**: `auto_tuner.py` adjusts parameters at end-of-day based on journal data

## Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `bot.py` | ~680 | Main `TradovateBot` class: start/stop, market data, signal execution, main loop |
| `tradovate_api.py` | ~1060 | `TradovateAPI` (REST), `MarketDataStream` (WebSocket), `RestMarketDataPoller` (Yahoo) |
| `strategies.py` | ~460 | `ORBStrategy` (dual-window breakout), `VWAPStrategy` (crossover with cooldown) |
| `risk_manager.py` | ~290 | `RiskManager`: trailing drawdown, daily loss brake, profit cap, position sizing |
| `config.py` | ~230 | All constants: credentials, URLs, challenge settings, contract specs, trading hours |
| `trade_journal.py` | ~480 | `TradeJournal`: record trades, per-symbol/strategy/hour analytics, lessons |
| `auto_tuner.py` | ~280 | `AutoTuner`: tune stops, targets, trade caps based on journal data |
| `dashboard.py` | ~600 | Flask web dashboard with embedded HTML/JS (RTL Hebrew layout) |
| `publish_dashboard.py` | ~660 | Static dashboard HTML generator, pushes to `docs/` for GitHub Pages |
| `status.py` | ~370 | Terminal dashboard with ANSI colors |
| `browser_bot.py` | ~460 | `BrowserTokenHarvester` (Playwright) + bot launcher |
| `get_token.py` | ~160 | One-time browser token capture script |
| `test_all.py` | ~1080 | Comprehensive test suite (auth, API, strategies, risk, E2E) |
| `setup_vps.sh` | ~250 | VPS auto-installer (Ubuntu/Debian): Python, venv, systemd, logrotate |

## Running

```bash
# Main bot
python bot.py              # Demo mode (default)
python bot.py --live       # Live mode
python bot.py --dry-run    # Paper mode — signals only, no orders

# Browser-based auth (Playwright)
python browser_bot.py                  # Opens browser, logs in, trades
python browser_bot.py --headless       # Headless browser
python browser_bot.py --dry-run        # Browser auth + paper mode

# One-time token capture (needs display for CAPTCHA)
python get_token.py

# Dashboard
python dashboard.py              # Flask server on port 8080
python dashboard.py --port 5000  # Custom port

# Static dashboard publish
python publish_dashboard.py          # One-time generate
python publish_dashboard.py --loop   # Auto-publish every 60s

# Terminal status
python status.py            # One-time snapshot
python status.py --watch    # Live refresh every 10s
python status.py --full     # Full report with journal + prices

# Trade journal report
python trade_journal.py            # Full report
python trade_journal.py --today    # Today only
python trade_journal.py --lessons  # Lessons & recommendations
```

## Testing

```bash
# Run the custom test suite (no pytest needed)
python test_all.py

# Or via pytest
python -m pytest test_all.py -v
```

Tests cover: password encryption, HMAC computation, token persistence, web auth flow, API-key auth, contract resolution, bracket order placement, WebSocket protocol, ORB strategy (single/dual window, warmup, stale breakouts), VWAP strategy (crossover, cooldown, multi-trade), risk manager (drawdown, daily loss, profit cap, position sizing), and end-to-end trading flow. All tests use mocks — no live token needed.

## Authentication

Token is stored in `.tradovate_token.json` and auto-renewed (~80 min expiry).

**Auth flow priority** (in `tradovate_api.py:authenticate()`):
1. `TRADOVATE_ACCESS_TOKEN` env var (manual override)
2. Pre-injected token via `set_token()`
3. Saved token from `.tradovate_token.json` (renewed via `/auth/renewaccesstoken`)
4. Web-style API auth (cid=8, no secret — reverse-engineered from Tradovate web trader JS)
5. API-key auth (CID + Secret)
6. Playwright browser login (handles CAPTCHA automatically)

**Web auth internals** (in `tradovate_api.py`):
- Password encrypted via `_encrypt_password()`: shift by `len(name) % len(password)`, reverse, base64
- HMAC-SHA256 `sec` field computed from `_HMAC_KEY` over `[chl, deviceId, name, password, appId]`
- Web app ID: `tradovate_trader(web)`, CID: `8`

**CAPTCHA handling**: FundedNext accounts require reCAPTCHA on first login from new devices. The bot uses Playwright headless browser to bypass this. Falls back to manual browser login via `get_token.py`.

## Trading Rules (FundedNext Challenge)

- Max trailing drawdown: $2,500 (trails unrealized intraday peaks)
- Daily loss limit: $1,000
- Profit target: $3,000
- Max contracts: 10 (minis)
- Close by: 4:59 PM ET
- **Consistency rule**: Max single-day profit = 40% of total profit needed
- **Daily profit cap**: $2,400 (enforced by risk manager)
- **Emergency brake**: Stops trading at 60% of daily loss limit used
- **Max daily trades**: 12 (hard cap across all symbols)
- **Risk per trade**: 1.5% of account size

## Trading Strategies

### ORB (Opening Range Breakout) — NQ, ES

Dual-window breakout strategy for equity index futures:
- **Window 1 (5-min)**: Fast, aggressive breakout — fires first after 9:30 AM ET
- **Window 2 (15-min)**: Wider range, stronger confirmation — fires later
- Max 2 trades per day per symbol, 15-min cooldown between trades
- Stop-loss at range boundary (capped at configured max), take-profit at 2:1 R:R

### VWAP Momentum — GC, CL

VWAP crossover strategy for commodity futures:
- Running VWAP calculated from cumulative typical price x volume
- Buy on confirmed cross above VWAP, sell on cross below
- Max 2 trades per direction per day, 30-min direction cooldown
- 5-min minimum gap between any trades (anti-whipsaw)
- Confirmation candles configurable (default: 1)

### Strategy Warmup

On late starts, `bot.py._warm_up_strategies()` fetches today's 1-min candles from Yahoo Finance and feeds them to strategies without executing signals. This builds ORB ranges and VWAP levels so the bot can start mid-session.

## Enabled Contracts

| Symbol | Name | Strategy | Stop | TP | Point Value | Tick |
|--------|------|----------|------|----|-------------|------|
| **NQ** | E-mini Nasdaq-100 | ORB (5m+15m) | 25pt | 50pt | $20 | 0.25 |
| **ES** | E-mini S&P 500 | ORB (5m+15m) | 6pt | 12pt | $50 | 0.25 |
| **GC** | Gold (COMEX) | VWAP | 5pt | 10pt | $100 | 0.10 |
| **CL** | WTI Crude Oil | VWAP | 0.20pt | 0.40pt | $1,000 | 0.01 |
| SI | Silver (disabled) | VWAP | 0.05pt | 0.10pt | $5,000 | 0.005 |
| NG | Natural Gas (disabled) | VWAP | 0.030pt | 0.060pt | $10,000 | 0.001 |

## Auto-Tuner

Runs at end-of-day via `auto_tuner.py`. Analyzes journal data and adjusts:
- **Stop-loss**: Widens if SL hit rate > 70%, tightens if < 30%
- **Take-profit**: Widens if avg R > 1.5, tightens if avg R < -0.5
- **Symbol allocation**: Flags symbols with <30% win rate AND <-$500 P&L for review
- **Daily trade cap**: Reduces if late trades (after 8th) are losing >70%

Safety bounds: max +/-20% adjustment per cycle, hard min/max per symbol. Changes logged to `tuner_log.json`.

## Risk Manager

`risk_manager.py:RiskManager` enforces all prop firm rules:
- **Trailing drawdown**: Updates `drawdown_floor` as peak equity rises (intraday for FundedNext)
- **Daily loss brake**: Locks trading at 60% of daily loss limit used
- **Daily profit cap**: Locks trading if day P&L exceeds $2,400 (consistency rule)
- **Position sizing**: `account_size * RISK_PER_TRADE_PCT / (stop_points * point_value)`, capped by available slots
- **Trade counter**: Hard cap at `MAX_DAILY_TRADES` (12)
- **New day detection**: Auto-resets daily counters at midnight

## Order Execution

Bracket orders use a two-step approach because FundedNext blocks `placeOSO`:
1. **Entry**: `POST /order/placeorder` (Market order)
2. **SL + TP**: `POST /order/placeOCO` (Stop + Limit, GTC)

Global 30-second cooldown between any two order placements to prevent rapid-fire.

## Market Data

Two modes (WebSocket preferred, REST fallback):
- **WebSocket** (`MarketDataStream`): Connects to `wss://md-demo.tradovateapi.com/v1/websocket` with custom text protocol. Auto-reconnects up to 5 times with exponential backoff.
- **REST Polling** (`RestMarketDataPoller`): Polls Yahoo Finance every 5 seconds for 1-min candles. Tracks last processed timestamp per symbol to avoid replaying data.

## API Notes

- FundedNext accounts use `organization: ""` (empty string, NOT "funded-next")
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket uses custom text protocol: `endpoint\nid\n\n{json}`
- Token expires ~80 minutes, auto-renewed via `/auth/renewaccesstoken`
- reCAPTCHA sitekey: `6Ld7FAoTAAAAAPdydZWpQ__C8xf29eYfvswcz52T`
- If demo auth fails, bot retries via live endpoint (some prop firms require it)

## Dashboard

### Flask Dashboard (`dashboard.py`)
- Runs on port 8080, auto-refreshes every 15 seconds
- RTL Hebrew layout
- Endpoints: `/api/status`, `/api/prices`, `/api/journal`, `/api/tuner`, `/api/log`, `/api/config`, `/api/refresh-token`
- Auto-authenticates on startup if no valid token exists
- Fetches live balance from Tradovate API when token is valid

### GitHub Pages Dashboard (`publish_dashboard.py`)
- Generates self-contained static HTML with all data embedded as JSON
- Publishes to `docs/index.html` for GitHub Pages
- Auto-refresh via `<meta http-equiv="refresh" content="60">`
- Loop mode: publishes every 60 seconds with git auto-commit/push

### GitHub Actions (`deploy-pages.yml`)
- Triggers on push to `docs/**` or manual dispatch
- Uses `peaceiris/actions-gh-pages@v4` to deploy `docs/` to `gh-pages` branch

## Deployment

### VPS Setup (`setup_vps.sh`)
One-command installer for Ubuntu/Debian:
1. Installs Python3 + system deps
2. Clones repo, creates venv, installs packages
3. Creates `.env` template
4. Installs systemd services (auto-start on boot)
5. Sets up log rotation

### systemd Services
- `tradovate-bot.service`: Runs `bot.py --live`, restarts on failure (30s delay), sends email alert on stop
- `tradovate-dashboard.service`: Runs `dashboard.py`, always restarts (10s delay)
- Alert script: `alert.sh` sends email via `mail`/`sendmail` when bot stops

## Dependencies

```
requests>=2.31.0
websocket-client>=1.7.0
python-dotenv>=1.0.0
numpy>=1.24.0
playwright>=1.40.0
```

Dashboard additionally requires `flask` (imported but not in requirements.txt).

## Configuration

All config is in `config.py`, loaded from `.env` via `python-dotenv`. See `.env.example` for all settings.

Key environment variables:
- `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD` — Required
- `TRADOVATE_ACCESS_TOKEN` — Manual token override (one-time)
- `TRADOVATE_ENV` — `demo` (default) or `live`
- `PROP_FIRM` — `fundednext`, `apex`, or `topstep`
- `TRADOVATE_ORGANIZATION` — Auto-set from prop firm (empty for FundedNext)
- `TRADOVATE_CID`, `TRADOVATE_SECRET` — Only for API-key auth
- `LOG_LEVEL`, `LOG_FILE` — Logging configuration

## Files to Never Commit

- `.env` — Credentials
- `.tradovate_token.json` — Auth tokens
- `*.log` — Log files
- `tuner_log.json` — Auto-tuner history (generated at runtime)

## Common Issues

1. **"Incorrect password"**: Credentials are correct; try `live` API endpoint (not `demo` for auth). The bot auto-retries via live endpoint.
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login. If that fails, run `get_token.py` on a machine with a display.
3. **Empty account list**: FundedNext challenge accounts are on demo API.
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth. Bot handles this automatically.
5. **`placeOSO` blocked**: FundedNext blocks OSO orders. Bot uses `placeorder` + `placeOCO` instead.
6. **WebSocket fails**: Bot falls back to REST polling via Yahoo Finance automatically.
7. **Dashboard missing `flask`**: Install with `pip install flask`.

## Development Conventions

- Python 3.10+ (uses `dict[str, ...]` type hints, `match` not used)
- No external framework — plain Python with `requests`, `websocket-client`, `numpy`
- All trading times in Eastern Time (ET), hardcoded UTC-5 offset
- Logging via stdlib `logging` — all modules use named loggers
- Config is module-level globals in `config.py` (mutable at runtime for auto-tuner)
- Tests use a custom `@test()` decorator + unittest.mock, compatible with both `python test_all.py` and `pytest`
- File paths use `os.path` or `pathlib.Path` relative to `__file__`
- No type checking enforced (no mypy/pyright config)
- No linter config (no ruff/flake8/black config files)
