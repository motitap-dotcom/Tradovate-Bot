# Tradovate Bot — הוראות ל-Claude Code

## חוקים קבועים (לא משתנים!)

### 1. שפה
- **תמיד לדבר בעברית** — כל תשובה, כל הסבר, כל שאלה — בעברית.

### 2. שם המשתמש
- **נועה** — לא מפתחת, צריכה הוראות פשוטות וברורות.
- אל תשתמש במונחים טכניים בלי הסבר.

### 3. תקשורת עם השרת — Push & Listen בלבד
- **אף פעם לא SSH ישיר** מהסביבה הזו.
- כל פעולה על השרת עוברת דרך **GitHub Actions** — ערוך קובץ, push, workflow רץ על השרת.
- אם מישהו מבקש "תבדוק את השרת" — **אל תגיד "אני לא יכול"**. תפעיל workflow.

### 4. מחשב מקומי
- **Windows** עם **PowerShell**
- כשנותנים הוראות לנועה — הוראות ל-PowerShell, לא bash.

### 5. שרת (VPS)
- **Contabo**, IP: `77.237.234.2`
- מערכת הפעלה: **Ubuntu Linux**
- תיקיית הבוט: `/root/tradovate-bot`
- Service: `tradovate-bot` (systemd)

### 6. טלגרם
- Bot Token: `8452836462:AAEVGDT5JrxOHAcB8Nd8ayObU1iMQUCRk2g`
- Chat ID: `7013213983`

---

## שיטת Push & Listen — איך זה עובד

כשצריך לבדוק / לתקן / לעדכן משהו על השרת, התהליך הוא:

```
1. לערוך קובץ בריפו (קוד, config, או קובץ trigger)
2. git commit + git push
3. GitHub Actions workflow רץ אוטומטית על השרת
4. התוצאות נכתבות חזרה לריפו (commit מהשרת)
5. git pull כדי לקרוא את התוצאות
```

### פעולות נפוצות — מה לעשות בכל מצב:

| בקשה | מה לעשות |
|------|----------|
| "תבדוק אם הבוט פעיל" | להפעיל `system-status` workflow (dispatch) או לבדוק `system_status.json` בריפו |
| "תתקן משהו על השרת" | לערוך את הקובץ הרלוונטי → push → auto-merge-deploy workflow מעדכן |
| "תעשה deploy" | לערוך קבצי קוד → push ל-main (או claude/* branch) → deploy workflow |
| "מה המצב?" | לבדוק `system_status.json` בריפו → אם ישן, להפעיל workflow |

### חשוב — אל תגיד "אני לא יכול לבדוק":
- אם מבקשים לבדוק את השרת → **תשתמש ב-workflow**
- אם אין workflow מתאים → **תיצור אחד**
- אם קובץ trigger לא קיים → **תיצור אותו**
- **תמיד תנסה לפעול**, לא רק להגיד "אי אפשר"

---

## Workflows קיימים

| Workflow | קובץ | מה עושה | מתי רץ |
|----------|-------|---------|--------|
| **CI — Tests & Lint** | `ci.yml` | בדיקות syntax + טסטים | push ל-main או claude/* |
| **Deploy to Server** | `deploy.yml` | מעדכן קוד על השרת, מתקין תלויות, מפעיל מחדש | push ל-main (לא md/docs) |
| **Auto-merge & Deploy** | `auto-merge-deploy.yml` | ממרג' מ-claude/* ל-main, ואז deploy לשרת | push ל-claude/* branches |
| **Connectivity Test** | `connectivity-test.yml` | בודק חיבור ל-API של Tradovate | כל 6 שעות + push ל-main |
| **Bot Status Check** | `server-health-check.yml` | קורא server_status.json ומציג סטטוס | כל 15 דקות + manual |
| **System Status Check** | `system-status.yml` | בדיקה מקיפה: API, auth, חשבון, פוזיציות, שרת | כל 30 דקות + manual |
| **Deploy Pages** | `deploy-pages.yml` | מעדכן dashboard ב-GitHub Pages | push לתיקיית docs/ |

### איך להפעיל workflow ידנית:
Workflows עם `workflow_dispatch` אפשר להפעיל ידנית:
```
gh workflow run "System Status Check"
gh workflow run "Deploy to Server"
gh workflow run "Tradovate Connectivity Test"
```

---

## פרטי הפרויקט — Tradovate Bot

### מה זה
בוט מסחר אוטומטי בחוזים עתידיים (Futures) על פלטפורמת **Tradovate**, עבור אתגר של **FundedNext**.

### פרטי חשבון
| פרט | ערך |
|------|------|
| Prop Firm | FundedNext (Futures Challenge) |
| Username | FNFTMOTITAPWnBks |
| Account | FNFTCHMOTITAPIRO67510 (Demo, id=39996695) |
| User ID | 5644210 |
| Organization | FundedNext (id=44) |
| Environment | demo (אתגר רץ על demo API) |
| Starting Balance | $50,000 |

### חוקי מסחר (FundedNext Challenge)
| חוק | ערך |
|------|------|
| Max trailing drawdown | $2,500 |
| Daily loss limit | $1,000 |
| Profit target | $3,000 |
| Max contracts | 10 (minis) |
| Close by | 4:59 PM ET |
| Drawdown | עוקב אחרי שיאים תוך-יומיים |

### חוזים פעילים
| חוזה | שם | אסטרטגיה | Stop | TP |
|---------|------|-----------|------|------|
| **NQ** | E-mini Nasdaq | ORB | 25pt | 50pt |
| **ES** | E-mini S&P | ORB | 6pt | 12pt |
| **GC** | Gold | VWAP | 5pt | 10pt |
| **CL** | Crude Oil | VWAP | 0.20pt | 0.40pt |

### מבנה הקוד
```
bot.py                  — אורקסטרטור ראשי: lifecycle, market data, ביצוע פקודות
├── tradovate_api.py    — REST + WebSocket client (auth, orders, positions, market data)
├── strategies.py       — יצירת סיגנלים: ORB (מדדים), VWAP (סחורות)
├── risk_manager.py     — ניהול סיכונים, גודל פוזיציות, הגבלות יומיות
├── config.py           — כל ההגדרות, נטען מ-.env
├── browser_bot.py      — כניסה דרך דפדפן + הפעלת בוט
├── trade_journal.py    — יומן עסקאות
├── auto_tuner.py       — כיוון אוטומטי
├── dashboard.py        — לוח בקרה
├── status.py           — סטטוס
└── get_token.py        — לכידת טוקן חד-פעמית
```

### קבצים חשובים
| קובץ | תפקיד |
|------|---------|
| `.env` | credentials והגדרות (אסור לעשות commit!) |
| `.tradovate_token.json` | טוקן מסחר (מתחדש אוטומטית) |
| `config.py` | מפרטי חוזים, הגבלות אתגר, שעות מסחר |
| `test_all.py` | סוויטת בדיקות |
| `server_status.json` | סטטוס מהשרת (מתעדכן כל 5 דקות) |
| `system_status.json` | סטטוס מקיף (מתעדכן כל 30 דקות ע"י workflow) |

### API Notes
- FundedNext משתמש ב-`organization: ""` (מחרוזת ריקה!)
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket: פרוטוקול טקסט מותאם: `endpoint\nid\n{json}`
- טוקן פג תוקף אחרי ~80 דקות, מתחדש דרך `/auth/renewaccesstoken`

---

## סטטוס עדכני

- **תאריך עדכון אחרון**: 2026-03-02
- **יתרה אחרונה ידועה**: ~$52,426 (נכון ל-2026-02-24)
- **שלב**: FundedNext Futures Challenge
- לבדיקת סטטוס עדכני — `system_status.json` בריפו

---

## How to Resume Work — איך להמשיך מחלון חדש

אם אתה חלון Claude חדש ולא יודע מה המצב:

1. **קרא את הקובץ הזה** — יש כאן את כל ההקשר.
2. **בדוק `system_status.json`** — לסטטוס API, auth, balance, פוזיציות.
3. **בדוק `server_status.json`** — לסטטוס השרת (bot active, memory, disk).
4. **בדוק git log** — מה השתנה לאחרונה.
5. **אם צריך סטטוס חי** — הפעל `System Status Check` workflow:
   ```
   gh workflow run "System Status Check"
   ```
   ואז חכה דקה ועשה `git pull` כדי לקרוא את `system_status.json` המעודכן.

### תזכורת:
- **דבר בעברית** עם נועה
- **אל תנסה SSH** — רק Push & Listen
- **אם לא יודע — תבדוק** (workflow, status files, git log)
- **push ל-main** מעדכן את השרת אוטומטית

---

## נוהל סיום משימה
בכל פעם שסיימת לכתוב קוד, לתקן באג או לבצע שינוי לבקשת המשתמש, בצע Push ל-main ודווח:
**"הקוד מוכן, ביצעתי Push ל-main כדי שהשרת יתעדכן."**
אל תריץ בדיקות אבחון (ping/curl/ssh) — הן לא רלוונטיות מהסביבה הזו.

## בדיקות
```bash
python -m pytest test_all.py -v
```

## בעיות נפוצות
1. **"Incorrect password"**: הסיסמה נכונה — נסה דרך `live` API (לא `demo` לאימות)
2. **CAPTCHA required**: הבוט מטפל אוטומטית דרך Playwright
3. **Empty account list**: חשבונות FundedNext הם על demo API
4. **Rate limiting (p-ticket)**: לחכות 15+ שניות לפני ניסיון חוזר
