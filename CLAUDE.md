# Tradovate Bot — Claude Code Guide

## חוקים קבועים (MANDATORY RULES)

> **חוקים אלה לא משתנים. כל חלון Claude חדש חייב לפעול לפיהם.**

1. **שפה**: תמיד לדבר בעברית. כל תשובה, הסבר, והודעה — בעברית בלבד.
2. **תקשורת עם השרת**: רק דרך GitHub Actions (Push & Listen). **אף פעם לא SSH ישיר.** ראה סעיף "Server Communication" למטה.
3. **המשתמשת**: נועה. היא לא מפתחת — צריך הוראות פשוטות, ברורות, ובעברית.
4. **מחשב מקומי**: Windows עם PowerShell.
5. **VPS**: Contabo, IP `77.237.234.2`, Ubuntu Linux.
6. **טלגרם**: Token `8452836462:AAEVGDT5JrxOHAcB8Nd8ayObU1iMQUCRk2g`, Chat ID `7013213983`.

---

## Server Communication

> **CRITICAL: NEVER use SSH, SCP, or any direct connection to the VPS.**
> **ONLY communicate with the server via GitHub Push & Listen through GitHub Actions.**

### איך זה עובד:
- כל שינוי בקוד → `git push` ל-`main`
- ה-VPS מאזין ל-webhook של GitHub ומושך שינויים אוטומטית
- לוגים והתראות מגיעים דרך טלגרם

### נוהל סיום משימה:
בכל פעם שסיימת לכתוב קוד, לתקן באג או לבצע שינוי לבקשת המשתמשת:
1. בצע `git push` ל-`main`
2. דווח: **"הקוד מוכן, ביצעתי Push ל-main כדי שהשרת יתעדכן."**
3. **אל תריץ בדיקות אבחון** (ping/curl/ssh) — הן לא רלוונטיות מהסביבה הזו.

---

## פרטי הפרויקט — Tradovate Trading Bot

### Account Info
- **Prop Firm**: FundedNext (Futures Challenge)
- **Username**: FNFTMOTITAPWnBks
- **Account**: FNFTCHMOTITAPIRO67510 (Demo, id=39996695)
- **User ID**: 5644210
- **Organization**: FundedNext (id=44)
- **Environment**: demo (challenge phase uses demo API)
- **Starting Balance**: $50,000

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

### API Notes
- FundedNext accounts use `organization: ""` (empty string, NOT "funded-next")
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket uses custom text protocol: `endpoint\nid\n{json}`
- Token expires ~80 minutes, auto-renewed via `/auth/renewaccesstoken`
- reCAPTCHA sitekey: `6Ld7FAoTAAAAAPdydZWpQ__C8xf29eYfvswcz52T`

### Testing
```bash
python -m pytest test_all.py -v
```

### Common Issues
1. **"Incorrect password"**: Credentials are correct; try `live` API (not `demo` for auth)
2. **CAPTCHA required**: Bot auto-handles via Playwright browser login
3. **Empty account list**: FundedNext challenge accounts are on demo API
4. **Rate limiting (p-ticket)**: Wait 15+ seconds before retrying auth

---

## סטטוס עדכני

| פרט | ערך |
|------|------|
| **באלנס נוכחי** | ~$52,426 |
| **שלב** | FundedNext Challenge |
| **סטטוס הבוט** | עובד תקין |
| **עדכון אחרון** | 2026-03-02 |

---

## How to Resume Work

> **סעיף זה נועד לחלון Claude חדש שנפתח ולא יודע מה הסטטוס.**

### צ'קליסט התחלה:
1. **קרא את כל ה-CLAUDE.md הזה** — הוא מכיל את כל ההקשר שאתה צריך.
2. **זכור את החוקים הקבועים** — עברית בלבד, אין SSH, נועה לא מפתחת.
3. **בדוק את סעיף הסטטוס העדכני למעלה** — שם תראה מה הבאלנס ומה הסטטוס.
4. **אם נועה מבקשת משהו** — בצע, עשה push ל-main, ודווח בעברית.

### מה לא לעשות:
- לא לנסות SSH לשרת
- לא לדבר באנגלית
- לא להניח שנועה מבינה מונחים טכניים — תסביר פשוט
- לא להריץ ping/curl/ssh — לא עובד מהסביבה הזו

### Deployment Flow:
```
קוד → git push to main → VPS webhook pulls automatically → Bot restarts
```
