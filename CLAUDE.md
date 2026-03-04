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
Push to `main` is the only way to deploy. The server webhook automatically pulls changes.

### נוהל סיום משימה
בכל פעם שסיימת לכתוב קוד, לתקן באג או לבצע שינוי לבקשת המשתמש, בצע Push ל-main ודווח: **"הקוד מוכן, ביצעתי Push ל-main כדי שהשרת יתעדכן."** אל תריץ בדיקות אבחון (ping/curl/ssh) – הן לא רלוונטיות מהסביבה הזו.

### עקרון עבודה חובה — Workflow-Based Operations

**אין גישה ישירה לשרת, ל-API או לסביבת הריצה מהסביבה הזו.**
**אין `gh` CLI.** הדרך היחידה לנהל את הבוט ולקבל מידע עדכני היא דרך
**GitHub Actions Workflows** + **GitHub REST API via WebFetch**.

### איך לקבל מידע עדכני:
1. **קרא `system_status.json` מ-main** דרך GitHub API:
   ```
   WebFetch: https://api.github.com/repos/motitap-dotcom/Tradovate-Bot/contents/system_status.json?ref=main
   ```
2. **אם המידע ישן** — עשה push כלשהו ל-`claude/*` branch (מפעיל system-status.yml אוטומטית)
3. **בדוק תוצאות workflow**:
   ```
   WebFetch: https://api.github.com/repos/motitap-dotcom/Tradovate-Bot/actions/workflows/240102669/runs?per_page=3
   ```

### Workflow IDs (לשימוש עם GitHub API):
| ID | Workflow | תדירות |
|----|----------|--------|
| 240102669 | System Status Check | כל 30 דקות + push |
| 239951353 | Bot Health Check | כל 15 דקות |
| 239953288 | Connectivity Test | כל 6 שעות |
| 239950089 | Auto-merge & Deploy | push ל-claude/* |

### חוקים:
- **אל תנסה** SSH, curl ישיר ל-Tradovate API, או `gh` CLI — לא עובד מכאן
- **אל תגיד** "אין לי גישה" — תשתמש ב-WebFetch לקרוא GitHub API
- **תמיד** תקרא קודם את `system_status.json` מ-main דרך GitHub API
- **Push ל-claude/* branch** = מפעיל auto-merge + deploy + system-status אוטומטית

## Common Issues
1. **"Incorrect password"**: Credentials are correct; try `live` API (not `demo` for auth)
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login
3. **Empty account list**: FundedNext challenge accounts are on demo API
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth
