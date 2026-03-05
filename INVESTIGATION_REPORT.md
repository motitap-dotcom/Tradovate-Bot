# Balance Investigation Report — March 5, 2026

## Summary

Analysis of `system_status.json` git history (18 authenticated snapshots from March 4, 2026) and `trade_journal.json` reveals:

**Total balance drop on March 4: $1,018.40** ($57,567.04 → $56,548.64)

---

## Balance Timeline (March 4, 2026)

| Time (UTC) | Time (ET) | Balance | Change | Trades Today |
|------------|-----------|---------|--------|--------------|
| 08:28 | 03:28 AM | $57,567.04 | — | 0 |
| 08:52 | 03:52 AM | $57,567.04 | — | 0 |
| 11:14 | 06:14 AM | $56,871.04 | **-$696.00** | 0 |
| 12:18 | 07:18 AM | $56,871.04 | — | 0 |
| 15:14 | 10:14 AM | $56,548.64 | **-$322.40** | 5 |
| 16:01-19:48 | 11:01 AM-2:48 PM | $56,548.64 | $0.00 | 5→7 |

---

## Drop #1: -$696.00 (between 03:52-06:14 AM ET)

### What happened:
- The bot was running with **environment="live"** at 08:28 UTC (3:28 AM ET)
- The bot's internal `live_status` showed `balance=50000` (the starting balance), while the API returned $57,567.04
- The bot was **restarted** between 08:52 and 11:14 UTC, now on **environment="demo"** (correct)
- When it restarted, the balance from the API was already $56,871.04

### Root cause:
The bot was briefly misconfigured with `TRADOVATE_ENV=live` instead of `demo`. This likely caused orders to be routed through the live API while the challenge account is on demo. The $696 loss may be from:
- Orders placed during the wrong-environment window
- Overnight settlement adjustments
- The exact trades are **not captured** in the logs from this window

---

## Drop #2: -$322.40 (between 07:18-10:14 AM ET)

### What happened:
The bot was running correctly on demo and made **5 trades** during the morning ORB (Opening Range Breakout) session.

### Signals detected in logs:
- **ORB 15-min range on NQ**: high=24911.50, low=24880.50, range=31 points
- **ORB NQ SHORT** signals at 24862.75 (window=15m, SL=24887.75 TP=24812.75)
- The bot generated trade signals between ~14:30-14:50 UTC (9:30-9:50 AM ET, right after market open)
- All 5 trades were executed and closed before the 15:14 UTC status check
- **No open positions** at any status check point → all trades were quick round-trips

### Strategy used: ORBStrategy (Opening Range Breakout)
- Traded NQ (E-mini Nasdaq) and likely ES (E-mini S&P)
- All trades were **SHORT** direction (betting on price going down)
- Stop losses were hit on most/all trades → market went UP after the open

---

## Afternoon Trades (trades 6-7): $0.00 P&L

| Time (UTC) | Time (ET) | Symbol | Direction | Strategy | Entry | SL | TP | Exit | P&L |
|------------|-----------|--------|-----------|----------|-------|-----|-----|------|-----|
| 18:16 | 1:16 PM | CL | Sell | VWAP | 74.88 | ? | ? | ? | ~$0 |
| 18:35 | 1:35 PM | CL | Sell | VWAP | 74.84 | 75.04 | 74.62 | 75.30 | ~$0* |
| 18:46 | 1:46 PM | CL | Buy | VWAP | 74.90 | 74.74 | 75.12 | ? | ~$0 |

*Note: The bot reported P&L=$0.00 for these trades, but the CL exit at 75.30 for a short entry should have been a loss. This is a **bug** in P&L tracking.

---

## Known Bugs Found

### 1. Entry price always recorded as 0
All trades in `trade_journal.json` show `entry_price: 0`. The journal records the entry but doesn't capture the actual fill price from the API.

### 2. P&L always reported as $0.00
The `day_pnl` in status logs is always `0.00` even when the balance clearly changed. The bot syncs the new balance from the API but doesn't properly calculate intraday P&L.

### 3. Environment misconfiguration
At 08:28 UTC, the bot was running with `environment="live"` instead of `demo`. This was corrected by 11:14 UTC when it was restarted.

### 4. Trade journal entries stuck as "open"
All 12 entries from Feb 23 in `trade_journal.json` remain as `status: "open"` with no exit data, even though positions were closed.

---

## Historical Context

| Date | Balance | Notes |
|------|---------|-------|
| Starting | $50,000 | FundedNext challenge start |
| Feb 23 (SOD) | ~$48,094 | After early losses |
| Feb 24 | ~$52,426 | Recovered |
| Mar 4 08:28 | $57,567 | Significant growth |
| Mar 4 11:14 | $56,871 | -$696 (environment issue?) |
| Mar 4 15:14 | $56,548 | -$322 (ORB shorts stopped out) |
| Mar 4 19:48 | $56,548 | No further change |
| Mar 5 (now) | ? | SSH connection lost, can't check |

**Overall P&L from start: +$6,548.64 (profit target is $3,000 — TARGET MET)**

---

## Recommendations

1. **Fix P&L tracking** — The `day_pnl=0.00` bug means the risk manager doesn't see intraday losses properly
2. **Fix entry_price recording** — Journal should capture fill price from API response
3. **Fix journal exit tracking** — Closed trades should be updated to `status: "closed"` with P&L
4. **Environment guard** — Add a startup check that verifies `TRADOVATE_ENV` matches the account type
5. **Investigate why SSH stopped working** — Since 20:00 UTC March 4, status checks show `auth=None` (can't read server token)
