# Trading Strategy Documentation

## Overview

This bot uses two strategies based on asset class:

| Asset Class | Strategy | Symbols |
|-------------|----------|---------|
| **Equity Indices** | ORB (Opening Range Breakout) — Dual Window | NQ, ES |
| **Commodities** | VWAP Momentum Crossover — Multi-trade | GC, CL, SI, NG |

---

## Strategy 1: Opening Range Breakout (ORB) — NQ, ES

### Logic — Dual Time Windows

The ORB now uses **two overlapping time windows** for safer frequency increase:

| Window | Duration | Fires After | Signal Quality |
|--------|----------|-------------|----------------|
| **5-minute** | 9:30–9:35 ET | 9:35 | Fast, aggressive — catches early momentum |
| **15-minute** | 9:30–9:45 ET | 9:45 | Wider range, stronger confirmation — catches delayed moves |

**How it works:**
1. **Window 1 (5-min)**: Builds the range from 9:30–9:35. If a breakout occurs, takes the trade.
2. **Window 2 (15-min)**: Builds a wider range from 9:30–9:45. Even if Window 1 already fired, Window 2 can produce a second trade if price breaks its (wider) range.
3. **Max 2 trades per symbol per day** (`max_orb_trades`).
4. **15-minute cooldown** between trades — prevents overtrading on volatile spikes.
5. **Stop loss**: At the opposite side of the range, capped at the configured max.
6. **Take profit**: At configured R/R ratio (1:2 default).

### Why Dual Windows Are Safe

- The 15-minute range is **always wider** than the 5-minute range, so a breakout of the 15-min range is a **stronger signal**.
- The cooldown ensures both trades aren't triggered in rapid succession.
- If the 5-min trade hits stop, the 15-min window provides a "second chance" with a wider range — effectively a filtered re-entry.

---

## Strategy 2: VWAP Momentum — GC, CL, SI, NG

### Logic — Multi-trade with Cooldown

1. **Calculate VWAP**: Running Volume-Weighted Average Price from session start.
2. **Long entry**: Price crosses above VWAP with a confirmed candle close → enter long.
3. **Short entry**: Price crosses below VWAP with a confirmed candle close → enter short.
4. **Multiple trades per direction**: Up to `max_vwap_trades_per_direction` (default: 2 for GC/CL, 1 for SI/NG).
5. **Cooldown between same-direction trades**: 30 minutes (GC/CL) or 60 minutes (SI/NG).
6. **Stop loss**: Just beyond VWAP.
7. **Take profit**: At configured distance.

### Why Multi-trade VWAP Is Safe

- VWAP doesn't reset during the day — each subsequent crossover represents a **genuine new price movement**, not noise.
- The 30-minute cooldown ensures the first trade is fully resolved (hit TP or SL) before a second trade is considered.
- Commodities often cross VWAP multiple times during a trending day. Allowing 2 trades catches the "real" move if the first was a false start.

---

## Recommended Stop/Profit Parameters by Symbol

| Symbol | Name | Tick Size | Tick Value | Stop Loss | Take Profit | R/R Ratio | Max Risk per Contract |
|--------|------|-----------|------------|-----------|-------------|-----------|----------------------|
| **NQ** | E-mini Nasdaq-100 | 0.25 pts | $5.00 | 25 pts | 50 pts | 1:2 | $500 |
| **ES** | E-mini S&P 500 | 0.25 pts | $12.50 | 6 pts | 12 pts | 1:2 | $300 |
| **GC** | Gold (COMEX) | $0.10 | $10.00 | 5.0 pts | 10.0 pts | 1:2 | $500 |
| **CL** | Crude Oil (NYMEX) | $0.01 | $10.00 | 0.20 pts | 0.40 pts | 1:2 | $200 |
| **SI** | Silver (COMEX) | $0.005 | $25.00 | 0.05 pts | 0.10 pts | 1:2 | $250 |
| **NG** | Natural Gas (NYMEX) | $0.001 | $10.00 | 0.030 pts | 0.060 pts | 1:2 | $300 |

---

## Expected Trade Frequency (Updated)

### Maximum per day (4 enabled symbols: NQ, ES, GC, CL)

| Symbol | Strategy | Max Trades/Day | Breakdown |
|--------|----------|----------------|-----------|
| **NQ** | ORB dual window | 2 | 1× from 5-min window + 1× from 15-min window |
| **ES** | ORB dual window | 2 | 1× from 5-min window + 1× from 15-min window |
| **GC** | VWAP multi-trade | 4 | 2 longs + 2 shorts (30-min cooldown between) |
| **CL** | VWAP multi-trade | 4 | 2 longs + 2 shorts (30-min cooldown between) |
| **Total** | | **12** | Hard cap enforced by `MAX_DAILY_TRADES = 12` |

### Realistic estimate

| Period | Before (old) | After (new) | Increase |
|--------|-------------|-------------|----------|
| **Typical day** | 2–4 trades | 4–8 trades | ~2× |
| **Quiet day** | 0–1 trades | 1–3 trades | ~2× |
| **Volatile day** | 4–6 trades | 6–10 trades | ~1.5× |
| **Week** | 10–20 trades | 20–40 trades | ~2× |
| **Month** | 40–80 trades | 80–160 trades | ~2× |

---

## Safety Compensations for Higher Frequency

Increasing trade frequency means more exposure. These safeguards compensate:

| Parameter | Before | After | Why |
|-----------|--------|-------|-----|
| `RISK_PER_TRADE_PCT` | 2.0% | **1.5%** | Smaller position per trade — total risk stays similar |
| `DAILY_LOSS_BRAKE_PCT` | 70% | **60%** | Tighter brake — locks trading earlier |
| `MAX_DAILY_TRADES` | (none) | **12** | Hard cap — prevents runaway trading |
| ORB cooldown | (none) | **15 min** | Prevents both ORB windows from firing simultaneously |
| VWAP cooldown | (none) | **30 min** | Ensures previous trade is resolved before re-entry |

### Risk Math: Before vs. After

**Before (max 6 trades × 2% risk each):**
- Worst-case daily risk: 6 × 2% = 12% of account = $6,000

**After (max 12 trades × 1.5% risk each, but with 60% brake):**
- Theoretical max: 12 × 1.5% = 18% of account = $9,000
- But the 60% brake locks trading after ~$600 loss (Topstep) or ~60% of drawdown distance (Apex)
- **Realistic worst case**: 4–5 consecutive losers before brake triggers = ~$3,000–$3,750
- This is **within** both Apex ($2,500 drawdown) and Topstep ($1,000 daily) limits because the brake fires at 60% of the limit

---

## Risk Management Rules

### Prop Firm Compliance

| Rule | Apex (50K) | Topstep (50K) |
|------|-----------|---------------|
| Max Trailing Drawdown | $2,500 (intraday unrealized) | $2,000 (end-of-day) |
| Daily Loss Limit | None | $1,000 |
| Profit Target | $3,000 | $3,000 |
| Max Contracts | 10 minis | 5 minis |
| Position Close By | 4:59 PM ET | 4:00 PM CT |

### Emergency Brake

The bot locks all trading when the daily P&L reaches **60%** of the daily loss limit:

- **Topstep**: Locks when day P&L hits **-$600** (60% of $1,000 daily limit).
- **Apex**: Locks when equity reaches 60% of the remaining distance to the drawdown floor.

### Position Sizing

```
Contracts = min(
    floor(Trade Risk Budget / Risk per Contract),
    Max Contracts - Open Contracts
)
```

Where:
- **Trade Risk Budget** = 1.5% of account size (configurable)
- **Risk per Contract** = Stop Loss (points) × Point Value per point

### All Orders Are Bracket Orders

Every entry order is automatically paired with:
- A **stop loss** order (Stop type)
- A **take profit** order (Limit type)

No "naked" market orders are ever sent.

---

## Trading Session Times

| Event | Time (ET) | Time (Israel) |
|-------|-----------|---------------|
| Market Open (ORB start) | 9:30 AM | 4:30 PM |
| ORB 5-min range closes | 9:35 AM | 4:35 PM |
| ORB 15-min range closes | 9:45 AM | 4:45 PM |
| New trades cutoff | 3:30 PM | 10:30 PM |
| Force close (Apex) | 4:59 PM | 11:59 PM |
| Force close (Topstep) | 4:00 PM CT | 11:00 PM |

---

## Adjustment Notes

- All stop/profit values in `config.py` are configurable per symbol.
- To disable a symbol, set `"enabled": False` in `CONTRACT_SPECS`.
- To revert to single-window ORB, change `"orb_windows": [5]` and `"max_orb_trades": 1`.
- To revert to single-trade VWAP, change `"max_vwap_trades_per_direction": 1`.
- For volatile days (FOMC, CPI, NFP), consider disabling the bot or widening stops manually.
- SI and NG are disabled by default due to high tick values and extreme volatility.
