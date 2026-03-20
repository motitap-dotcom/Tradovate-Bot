# Tradovate Bot — Claude Code Guide

## כלל ברזל #0 — אסור לנחש, חובה לשלוף

**לפני כל תשובה על מצב הבוט, הבאלנס, טרייד פתוח, או כל שאלה תפעולית — חובה לשלוף מידע אמיתי.**

- חובה להציג `timestamp` של המידע
- אם ה-timestamp ישן מ-30 דקות — להזהיר במפורש: "המידע ישן מ-XX דקות, ייתכן שהמצב השתנה"
- אסור להסתמך על מידע מריצה קודמת, מ-context ישן, או מזיכרון
- אסור לאשר שינוי הצליח בלי לבדוק `system_status.json` אחרי דפלוי
- אסור להמציא מספרי באלנס, P&L, או מצב פוזיציות

---

## כלל ברזל #1 — Workflow Only (אין גישה ישירה לשרת)

**אין SSH, אין ping, אין curl לשרת, אין `gh` CLI.**
הדרך היחידה לנהל את הבוט: **Git Push → GitHub Actions → VPS**.

### מה מותר
- לערוך קוד, לעשות commit, ולדחוף ל-`claude/*` branch
- לקרוא קבצי סטטוס מ-GitHub API דרך WebFetch
- לבדוק תוצאות workflows דרך GitHub API

### מה אסור
- ❌ SSH לשרת
- ❌ ping / curl / network diagnostics לשרת
- ❌ קריאות API ישירות ל-Tradovate מהסביבה הזו
- ❌ `gh` CLI (לא מותקן)
- ❌ לקרוא לוגים של השרת ישירות — הם מגיעים רק דרך `server_status.json`
- ❌ לומר "אין לי גישה" — תשתמש ב-WebFetch

---

## ארכיטקטורה: Git → Actions → VPS

```
┌─────────────────────────────────────────────────────────────┐
│                    הזרימה המלאה                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ① PUSH ל-claude/* branch                                  │
│     ↓                                                       │
│  ② auto-merge-deploy.yml                                    │
│     → מריץ טסטים (ci.yml)                                   │
│     → ממזג ל-main                                           │
│     ↓                                                       │
│  ③ deploy.yml (על push ל-main)                              │
│     → SSH לשרת                                              │
│     → git pull origin main                                  │
│     → pip install -r requirements.txt                       │
│     → systemctl restart tradovate-bot                       │
│     → מעדכן crontab (server_cron.sh כל 5 דקות)             │
│     ↓                                                       │
│  ④ server_cron.sh (כל 5 דקות על השרת)                       │
│     → בודק עדכוני קוד + auto-heal                           │
│     → כותב server_status.json                               │
│     → דוחף ל-GitHub דרך API                                 │
│     ↓                                                       │
│  ⑤ system-status.yml (כל 30 דקות)                           │
│     → SSH לשרת → אוסף סטטוס מלא                            │
│     → כותב system_status.json ל-main                        │
│     ↓                                                       │
│  ⑥ server-health-check.yml (כל 15 דקות בשעות מסחר)         │
│     → קורא server_status.json                               │
│     → אם הבוט למטה → auto-restart דרך SSH                   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### השרת (VPS)
- **נתיב הבוט**: `/root/tradovate-bot`
- **Service**: `tradovate-bot` (systemd)
- **הרצה**: `/root/tradovate-bot/venv/bin/python bot.py`
- **Restart**: אוטומטי כל 30 שניות על כשל
- **Memory limit**: 2GB (בגלל Playwright/Chrome)
- **לוגים**: `journalctl -u tradovate-bot`
- **Cron log**: `/var/log/tradovate-cron.log`
- **Status path**: `/var/bots/Tradovate_status.json` (כתיבה מקומית ע"י `status_reporter.py`)

---

## קבצי הפרויקט — מפה מלאה

### קבצי ליבה (Bot Core)
| קובץ | תפקיד |
|------|--------|
| `bot.py` | אורקסטרטור ראשי: lifecycle, market data, ביצוע פקודות |
| `tradovate_api.py` | REST + WebSocket client: auth, orders, positions, market data |
| `strategies.py` | אסטרטגיות מסחר: ORB (מדדים), VWAP (סחורות) |
| `risk_manager.py` | ניהול סיכונים: trailing drawdown, daily loss, position sizing |
| `config.py` | כל ההגדרות: API URLs, חוקי challenge, contract specs |
| `trade_journal.py` | יומן מסחר: ביצועים, win rate, דוחות יומיים |
| `auto_tuner.py` | אופטימיזציה אוטומטית של פרמטרים לפי היסטוריה |

### קבצי תמיכה (Supporting)
| קובץ | תפקיד |
|------|--------|
| `browser_bot.py` | כניסה חלופית: auth דרך דפדפן + הפעלת בוט |
| `dashboard.py` | דשבורד ווב למוניטורינג |
| `status.py` | endpoint סטטוס ודיווח |
| `status_reporter.py` | כותב סטטוס ל-`/var/bots/Tradovate_status.json` |
| `bot_state.py` | שמירת מצב בין הפעלות מחדש |
| `bot_health_check.py` | בדיקת תקינות (רץ כל 5 דקות דרך cron) |
| `connection_check.py` | בדיקת connectivity ל-API |
| `verify_bot.py` | סקריפט אימות מקיף של הבוט |
| `check_server.py` | בדיקת סטטוס שרת מרחוק |
| `check_account.py` | שליפת מידע חשבון |
| `monitor.py` | כלי מוניטורינג |
| `publish_dashboard.py` | פרסום דשבורד ל-GitHub Pages |
| `get_token.py` | לכידת טוקן חד-פעמית (צריך display) |

### סקריפטים לשרת
| קובץ | תפקיד |
|------|--------|
| `server_cron.sh` | רץ כל 5 דקות: pull, auto-heal, סטטוס, push ל-GitHub |
| `setup_vps.sh` | התקנה אוטומטית של VPS (Ubuntu/Debian) |
| `keep_alive.sh` | fallback: restart loop אינסופי |
| `alert.sh` | התראה כשהבוט נעצר |
| `tradovate-bot.service` | systemd service definition |

### קבצי סטטוס (מתעדכנים אוטומטית)
| קובץ | מקור עדכון | תדירות |
|------|-----------|---------|
| `system_status.json` | GitHub Actions (`system-status.yml`) | כל 30 דקות |
| `server_status.json` | server cron (`server_cron.sh`) | כל 5 דקות |
| `live_status.json` | תהליך הבוט עצמו | בזמן אמת |
| `bot_health.json` | `bot_health_check.py` | כל 5 דקות |
| `connection_status.json` | `connection_check.py` | לפי הפעלה |
| `server_manage_result.json` | `server-manage.yml` | manual dispatch |
| `server_report.json` | `deploy-loop.yml` | על כל deploy |
| `trade_journal.json` | `trade_journal.py` | כל סגירת טרייד |

### קבצי config (לא ב-Git)
| קובץ | תפקיד |
|------|--------|
| `.env` | credentials + הגדרות סביבה (לעולם לא לעשות commit) |
| `.tradovate_token.json` | טוקן auth שמור (מתחדש אוטומטית) |
| `.gh_pat` | GitHub PAT לשימוש ה-cron (על השרת בלבד) |

### טסטים
| קובץ | תפקיד |
|------|--------|
| `test_all.py` | test suite מלא — `python -m pytest test_all.py -v` |

---

## GitHub Actions Workflows — רשימה מלאה

### Workflows אוטומטיים (Core Pipeline)
| Workflow | קובץ | טריגר | תפקיד |
|----------|------|--------|--------|
| **Auto-merge & Deploy** | `auto-merge-deploy.yml` | Push ל-`claude/**` | טסטים → מיזוג ל-main → deploy |
| **Deploy to Server** | `deploy.yml` | Push ל-main | syntax check → SSH deploy → restart service → cron setup |
| **Deploy → Listen** | `deploy-loop.yml` | Push ל-main/claude/** | deploy → collect feedback → commit `server_report.json` |
| **CI — Tests & Lint** | `ci.yml` | Push/PR ל-main | Python 3.11+3.12 syntax check + pytest |

### Workflows מוניטורינג (Health & Status)
| Workflow | קובץ | טריגר | תפקיד |
|----------|------|--------|--------|
| **System Status Check** | `system-status.yml` | כל 30 דקות + push | SSH → סטטוס מלא → כותב `system_status.json` |
| **Bot Health Check** | `server-health-check.yml` | כל 15 דקות (שעות מסחר) | קורא `server_status.json` → auto-restart אם למטה |
| **Tradovate Connectivity Test** | `connectivity-test.yml` | כל 6 שעות + push | בדיקת DNS + API endpoints (demo + live) |
| **Daily Trade Report** | `trade-report.yml` | 22:00 UTC (17:00 ET) ימי חול | דוח מסחר יומי → GitHub Issue |

### Workflows ידניים (Manual Dispatch)
| Workflow | קובץ | טריגר | תפקיד |
|----------|------|--------|--------|
| **Quick Bot Check** | `bot-check.yml` | ידני | SSH מהיר: תהליך, לוגים, טוקן, משאבים |
| **Server Management** | `server-manage.yml` | ידני | פקודות: status, restart-bot, bot-logs, check-trades, fix-bot, cron-status, full-diagnostic |
| **Deploy Dashboard** | `deploy-pages.yml` | Push ל-docs/** | פרסום דשבורד ל-GitHub Pages |

### Workflow IDs (לשימוש עם GitHub API)
| ID | Workflow | שימוש |
|----|----------|-------|
| 240102669 | System Status Check | `system_status.json` |
| 239951353 | Bot Health Check | auto-restart |
| 239953288 | Connectivity Test | API health |
| 239950089 | Auto-merge & Deploy | pipeline ראשי |

---

## פקודות שאפשר לתת ל-Claude Code — ומה קורה בפועל

### "מה מצב הבוט?"
1. שולף `system_status.json` מ-main דרך GitHub API:
   ```
   WebFetch: https://api.github.com/repos/motitap-dotcom/Tradovate-Bot/contents/system_status.json?ref=main
   ```
2. מציג: `timestamp`, `bot_active`, `balance`, `day_pnl`, `locked`, `lock_reason`
3. אם ה-timestamp ישן מ-30 דקות — מזהיר

### "מה הבאלנס / P&L?"
- שולף `system_status.json` → מציג `balance.totalCashValue`, `live_status.day_pnl`, `live_status.drawdown_floor`

### "יש פוזיציות פתוחות?"
- שולף `system_status.json` → בודק `positions` ו-`orders`

### "תעדכן / תתקן / תשנה [קוד]"
1. עורך את הקבצים הרלוונטיים
2. `git commit` עם הודעה ברורה
3. `git push -u origin claude/<branch-name>`
4. Auto-merge → Deploy → Server restart
5. אחרי ~5 דקות: בודק `system_status.json` לאישור

### "תפעיל מחדש את הבוט"
- Push שינוי כלשהו (אפילו comment) ל-`claude/*` → auto-merge → deploy → restart
- או: מפעיל `server-manage.yml` עם command=restart-bot (דרך push שמשנה את הקובץ)

### "תבדוק לוגים"
- שולף `system_status.json` → מציג `journal_tail` (50 שורות אחרונות)
- או שולף `server_status.json` → מציג `health_check`

### "תריץ טסטים"
```bash
python -m pytest test_all.py -v
```

### "מה הסטטוס של ה-workflows?"
```
WebFetch: https://api.github.com/repos/motitap-dotcom/Tradovate-Bot/actions/workflows/240102669/runs?per_page=3
```

---

## פרוטוקול עבודה — חובה בכל session

### צעד ראשון: בדיקת סטטוס
```
WebFetch: https://api.github.com/repos/motitap-dotcom/Tradovate-Bot/contents/system_status.json?ref=main
```
לבדוק: `timestamp`, `bot_active`, `balance`, `day_pnl`, `locked`, `lock_reason`

### אחרי כל שינוי קוד:
1. **Commit** עם הודעה ברורה
2. **Push** ל-`claude/*` branch
3. **דיווח**: "הקוד מוכן, ביצעתי Push ל-claude/* כדי שהשרת יתעדכן."
4. **בדיקה**: שולף `system_status.json` אחרי ~5 דקות לאימות

### אם המידע ישן:
- Push כלשהו ל-`claude/*` מפעיל `system-status.yml` אוטומטית
- מחכים ~2-3 דקות ושולפים שוב

---

## מידע על החשבון

- **Prop Firm**: FundedNext (Futures Challenge)
- **Username**: FNFTMOTITAPWnBks
- **Account**: FNFTCHMOTITAPIRO67510 (Demo, id=39996695)
- **User ID**: 5644210
- **Organization**: FundedNext (id=44)
- **Environment**: demo (challenge phase uses demo API)
- **Starting Balance**: $50,000

## חוקי מסחר (FundedNext Challenge)
- **Max trailing drawdown**: $2,500 (עוקב אחרי שיאים תוך-יומיים)
- **Daily loss limit**: $1,000
- **Daily profit cap**: $2,400 (כלל עקביות — מקסימום 40% מהרווח המצטבר ביום אחד)
- **Profit target**: $3,000
- **Max contracts**: 40 (micro slots; 10 per symbol × 4 symbols)
- **סגירת פוזיציות**: עד 16:59 ET

## חוזים פעילים (Micro Contracts)
| חוזה | אסטרטגיה | Stop | TP | $/pt | כמות מקס |
|-------|----------|------|-----|------|----------|
| **MNQ** (Micro Nasdaq) | ORB | 25pt | 50pt | $2 | 10 |
| **MES** (Micro S&P) | ORB | 6pt | 12pt | $5 | 10 |
| **MGC** (Micro Gold) | VWAP | 5pt | 10pt | $10 | 10 |
| **MCL** (Micro Crude) | VWAP | 0.20pt | 0.40pt | $100 | 10 |

---

## Authentication

טוקן שמור ב-`.tradovate_token.json` ומתחדש אוטומטית (פג כל ~80 דקות).

**סדר עדיפויות** (ב-`tradovate_api.py:authenticate()`):
1. `TRADOVATE_ACCESS_TOKEN` env var (override ידני)
2. טוקן מוזרק דרך `set_token()`
3. טוקן שמור מ-`.tradovate_token.json` (חידוש דרך API)
4. Web-style API auth (cid=8)
5. API-key auth (CID + Secret HMAC)
6. Playwright browser login (מטפל ב-CAPTCHA אוטומטית)

---

## API Notes
- FundedNext: `organization: ""` (מחרוזת ריקה, לא "funded-next")
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket trading: `wss://demo.tradovateapi.com/v1/websocket`
- WebSocket market data: `wss://md-demo.tradovateapi.com/v1/websocket`
- פרוטוקול WS: `endpoint\nid\n{json}` (text, לא binary)
- חידוש טוקן: `/auth/renewaccesstoken`
- הזמנות: `/placeOSO` (bracket: entry + SL + TP), `isAutomated: true` (חובת CME)

---

## בעיות נפוצות
1. **"Incorrect password"**: הקרדנשיאלס תקינים — לנסות `live` API (לא `demo` ל-auth)
2. **CAPTCHA required**: הבוט מטפל אוטומטית דרך Playwright
3. **Empty account list**: חשבונות FundedNext challenge נמצאים ב-demo API
4. **Rate limiting (p-ticket)**: לחכות 15+ שניות לפני retry
5. **False daily profit cap lock**: אם `_init_balance_from_api()` נכשל בהפעלה (טוקן פג), `day_start_balance` מקבל ברירת מחדל $50K מה-config במקום הבאלנס האמיתי → `day_pnl` מנופח. תוקן: `_sync_balance()` מנסה שוב `set_initial_balance()` בקריאת API מוצלחת ראשונה (דגל `_balance_initialized` ב-`risk_manager.py`).

---

## מה אסור לעשות — סיכום

1. **לנחש** מצב הבוט, באלנס, או P&L — חובה לשלוף מידע אמיתי
2. **להסתמך על ריצה קודמת** — כל session מתחיל בשליפה טרייה
3. **לאשר שהדפלוי הצליח** בלי לבדוק `system_status.json`
4. **לגשת לשרת ישירות** — רק דרך Git → Actions → VPS
5. **להציג מידע בלי timestamp** — תמיד להראות מתי המידע נאסף
6. **להתעלם ממידע ישן** — אם עברו 30+ דקות, להזהיר
7. **להמציא URLs** — רק URLs ידועים מהמסמך הזה
8. **לעשות commit ל-.env או לקבצי credentials**
