# Tradovate Bot — הוראות ל-Claude Code

## חוקים קבועים (לא משתנים!)

### 1. שפה
- **תמיד לדבר בעברית** — כל תשובה, כל הסבר, כל שאלה — בעברית.

### 2. תקשורת עם השרת — רק Push & Listen
- **אף פעם לא SSH ישיר** — כל תקשורת עם ה-VPS רק דרך GitHub Actions.
- אם צריך לבדוק/לתקן/לעדכן משהו על השרת — Push לריפו, ה-workflow יטפל.
- אם אין workflow מתאים — **תיצור אחד**.

### 3. מי המשתמשת
- **שם**: נועה
- **לא מפתחת** — צריכה הוראות פשוטות וברורות, בלי ז'רגון טכני מיותר.
- אם משהו דורש פעולה מצד נועה, להסביר צעד-צעד.

### 4. סביבת עבודה מקומית
- **מחשב**: Windows עם PowerShell
- **לא Linux** — פקודות צריכות להיות תואמות PowerShell/Windows.

### 5. שרת (VPS)
- **ספק**: Contabo
- **IP**: `77.237.234.2`
- **מערכת הפעלה**: Ubuntu Linux
- **תיקיית הבוט**: `/root/tradovate-bot`
- **שירות**: `tradovate-bot` (systemd)

### 6. טלגרם (התראות)
- **Bot Token**: `8452836462:AAEVGDT5JrxOHAcB8Nd8ayObU1iMQUCRk2g`
- **Chat ID**: `7013213983`

---

## שיטת Push & Listen — איך זה עובד

כשצריך לבדוק/לתקן/לעדכן משהו על השרת, התהליך הוא:

```
1. לערוך קובץ בריפו (קוד, config, או קובץ trigger)
2. git commit + git push
3. GitHub Actions workflow רץ אוטומטית על השרת
4. התוצאות נכתבות חזרה לריפו (commit מהשרת)
5. git pull כדי לקרוא את התוצאות
```

### פעולות נפוצות — מה לעשות בכל מצב

| נועה אומרת | מה לעשות |
|-------------|----------|
| "תבדוק אם הבוט פעיל" | לערוך `trigger-check.txt` עם תאריך/שעה → push → workflow בודק ומחזיר תוצאה |
| "תתקן משהו על השרת" | לערוך את הקובץ הרלוונטי → push → workflow מריץ את התיקון |
| "תעשה deploy" | לערוך קבצי קוד → push → deploy workflow מעדכן על השרת |
| "מה המצב?" | לבדוק `system_status.json` / `server_status.json` בריפו → אם מיושן, להפעיל trigger check |

### חשוב — אל תגיד "אני לא יכול לבדוק"!
- אם מבקשים ממך לבדוק את השרת — **תשתמש ב-workflow**
- אם אין workflow מתאים — **תיצור אחד**
- אם `trigger-check.txt` לא קיים — **תיצור אותו**
- **תמיד תנסה לפעול**, לא רק להגיד "אי אפשר"

---

## Workflows קיימים

רשימת ה-workflows בתיקיית `.github/workflows/`:

| Workflow | קובץ | מה עושה | מתי רץ |
|----------|-------|---------|--------|
| **Auto-merge & Deploy** | `auto-merge-deploy.yml` | ממזג claude/* branches ל-main ועושה deploy לשרת דרך SSH | push ל-`claude/**` |
| **CI — Tests & Lint** | `ci.yml` | בדיקות סינטקס + הרצת טסטים (Python 3.11, 3.12) | push ל-main/claude/**, PR ל-main |
| **Connectivity Test** | `connectivity-test.yml` | בודק חיבור ל-Tradovate API (Live + Demo) + אימות credentials | push ל-main, כל 6 שעות, ידני |
| **Deploy Pages** | `deploy-pages.yml` | מעדכן דשבורד ב-GitHub Pages מתיקיית `docs/` | push שמשנה `docs/**`, ידני |
| **Deploy to Server** | `deploy.yml` | מושך קוד חדש לשרת, מעדכן dependencies, מפעיל מחדש את הבוט | push ל-main (לא md/docs), ידני |
| **Bot Status Check** | `server-health-check.yml` | קורא `server_status.json` ומציג סטטוס מפורט | כל 15 דקות, push ל-main/claude/**, ידני |
| **System Status Check** | `system-status.yml` | בדיקת מערכת מקיפה: API, אימות, חשבון, יתרה, פוזיציות — כותב `system_status.json` | כל 30 דקות, push ל-main/claude/**, ידני |

---

## פרטי הפרויקט — Tradovate Trading Bot

### חשבון מסחר
- **Prop Firm**: FundedNext (Futures Challenge)
- **שם משתמש**: FNFTMOTITAPWnBks
- **חשבון**: FNFTCHMOTITAPIRO67510 (Demo, id=39996695)
- **User ID**: 5644210
- **ארגון**: FundedNext (id=44)
- **סביבה**: demo (שלב challenge משתמש ב-demo API)
- **יתרת התחלה**: $50,000

### חוקי מסחר (FundedNext Challenge)
- **Max trailing drawdown**: $2,500
- **Daily loss limit**: $1,000
- **יעד רווח**: $3,000
- **מקסימום חוזים**: 10 (minis)
- **סגירה עד**: 4:59 PM ET
- Drawdown עוקב אחרי שיאים תוך-יומיים (unrealized)

### חוזים פעילים

| חוזה | שם | אסטרטגיה | Stop | Take Profit |
|------|----|-----------|------|-------------|
| **NQ** | E-mini Nasdaq | ORB | 25pt | 50pt |
| **ES** | E-mini S&P | ORB | 6pt | 12pt |
| **GC** | Gold | VWAP | 5pt | 10pt |
| **CL** | Crude Oil | VWAP | 0.20pt | 0.40pt |

### ארכיטקטורה

```
bot.py                  — אורקסטרטור ראשי: מחזור חיים, נתוני שוק, ביצוע פקודות
├── tradovate_api.py    — לקוח REST + WebSocket (אימות, פקודות, פוזיציות, נתוני שוק)
├── strategies.py       — יצירת סיגנלים: ORB (מדדים), VWAP (סחורות)
├── risk_manager.py     — אכיפת drawdown, גודל פוזיציה, מגבלות הפסד יומי
├── config.py           — כל ההגדרות, נטען מ-.env
├── browser_bot.py      — נקודת כניסה חלופית: אימות דפדפן + בוט
└── get_token.py        — לכידת טוקן חד-פעמית (דורש display)
```

### קבצים חשובים

| קובץ | תפקיד |
|-------|--------|
| `.env` | credentials והגדרות (לעולם לא לעשות commit!) |
| `.tradovate_token.json` | טוקן אימות שמור (מתחדש אוטומטית) |
| `config.py` | מפרטי חוזים, מגבלות challenge, שעות מסחר |
| `tradovate_api.py` | לקוח Tradovate API מלא |
| `strategies.py` | אסטרטגיות ORB + VWAP |
| `risk_manager.py` | גודל פוזיציה + הגנת drawdown |
| `test_all.py` | סוויטת בדיקות |
| `server_status.json` | סטטוס שרת (מתעדכן כל 5 דקות מה-cron) |
| `system_status.json` | סטטוס מערכת מקיף (מתעדכן כל 30 דקות מ-workflow) |

### API Notes
- חשבונות FundedNext משתמשים ב-`organization: ""` (מחרוזת ריקה, לא "funded-next")
- Demo API: `https://demo.tradovateapi.com/v1`
- Live API: `https://live.tradovateapi.com/v1`
- WebSocket משתמש בפרוטוקול טקסט: `endpoint\nid\n{json}`
- טוקן פג תוקף אחרי ~80 דקות, מתחדש דרך `/auth/renewaccesstoken`

### אימות (Auth Flow)
סדר עדיפות ב-`tradovate_api.py:authenticate()`:
1. `TRADOVATE_ACCESS_TOKEN` env var (override ידני)
2. טוקן מוזרק דרך `set_token()`
3. טוקן שמור מ-`.tradovate_token.json` (מחודש דרך API)
4. אימות Web-style (cid=8, בלי secret)
5. אימות API-key (CID + Secret)
6. התחברות דרך Playwright (מטפל ב-CAPTCHA אוטומטית)

---

## סטטוס עדכני

- **תאריך עדכון**: 2026-03-02
- **יתרה אחרונה**: ~$52,426 (נכון ל-2026-02-24)
- לסטטוס מעודכן: לבדוק `system_status.json` בריפו, או להפעיל System Status workflow ידנית

---

## How to Resume Work — איך חלון חדש ממשיך עבודה

כשנפתח חלון Claude חדש, זה מה שצריך לעשות כדי להמשיך:

### 1. להבין את המצב הנוכחי
```bash
git pull origin main              # למשוך שינויים אחרונים
git log --oneline -10             # לראות מה השתנה לאחרונה
```

### 2. לבדוק סטטוס הבוט
- לקרוא את `system_status.json` — מכיל סטטוס מערכת מלא
- לקרוא את `server_status.json` — מכיל סטטוס שרת
- אם הקבצים ישנים (יותר מ-30 דקות) — להפעיל workflow ידנית דרך GitHub Actions

### 3. לזכור את החוקים
- **עברית** תמיד
- **Push & Listen** — לא SSH ישיר
- נועה **לא מפתחת** — הוראות פשוטות
- **לפעול**, לא להגיד "אי אפשר"

### 4. לבדוק אם יש משימות פתוחות
- לקרוא את ההיסטוריה האחרונה בריפו
- לבדוק אם יש branches פתוחים
- לבדוק אם יש GitHub Actions שנכשלו

---

## נוהל סיום משימה

בכל פעם שסיימת לכתוב קוד, לתקן באג או לבצע שינוי לבקשת נועה:
1. **Push ל-main** (דרך claude/* branch שמתמזג אוטומטית)
2. **דווח**: "הקוד מוכן, ביצעתי Push כדי שהשרת יתעדכן."
3. **אל תריץ בדיקות אבחון** (ping/curl/ssh) — הן לא רלוונטיות מהסביבה הזו

---

## בעיות נפוצות

| בעיה | פתרון |
|------|--------|
| "Incorrect password" | ה-credentials נכונים; לנסות `live` API (לא `demo` לאימות) |
| CAPTCHA required | הבוט מטפל אוטומטית דרך Playwright |
| רשימת חשבונות ריקה | חשבונות FundedNext Challenge הם ב-demo API |
| Rate limiting (p-ticket) | לחכות 15+ שניות לפני ניסיון חוזר |

## בדיקות
```bash
python -m pytest test_all.py -v
```
