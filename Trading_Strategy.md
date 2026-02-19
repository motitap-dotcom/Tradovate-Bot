# Trading Strategy Documentation

## Overview

This bot uses two strategies based on asset class:

| Asset Class | Strategy | Symbols |
|-------------|----------|---------|
| **Equity Indices** | ORB (Opening Range Breakout) | NQ, ES |
| **Commodities** | VWAP Momentum Crossover | GC, CL, SI, NG |

---

## Strategy 1: Opening Range Breakout (ORB) — NQ, ES

### Logic

1. **Mark the range**: Record the high and low of the first 5 minutes after the US market open (9:30 AM ET / 16:30 Israel time).
2. **Long entry**: A candle closes above the range high → enter long at market.
3. **Short entry**: A candle closes below the range low → enter short at market.
4. **Stop loss**: Placed at the opposite side of the opening range (or capped at the configured max).
5. **Take profit**: Set at the configured risk/reward ratio from entry.
6. **One trade per day per symbol**: After the first breakout trade, the strategy stops for that symbol.

### Why ORB Works for Indices

The 9:30 AM open is the highest-volume period of the day. Institutional order flow creates a range in the first 5 minutes that often defines the direction for the session. Breaking out of this range with conviction (candle close) signals a directional move.

---

## Strategy 2: VWAP Momentum — GC, CL, SI, NG

### Logic

1. **Calculate VWAP**: Running Volume-Weighted Average Price from session start.
2. **Long entry**: Price crosses above VWAP with a confirmed candle close → enter long.
3. **Short entry**: Price crosses below VWAP with a confirmed candle close → enter short.
4. **Stop loss**: Placed just beyond the VWAP line (stop_loss_points below VWAP for longs, above for shorts).
5. **Take profit**: Set at the configured take_profit_points distance.
6. **One trade per direction per day**: Max one long and one short signal per session.

### Why VWAP Works for Commodities

VWAP represents the "fair value" where most volume has traded. When price decisively crosses VWAP, it signals a shift in institutional sentiment. Commodities tend to trend strongly once they break from the mean.

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

### Max Risk per Contract Calculation

```
Max Risk = Stop Loss (in points) × Point Value
```

| Symbol | Formula | Result |
|--------|---------|--------|
| NQ | 25 × $20 | $500 |
| ES | 6 × $50 | $300 |
| GC | 5.0 × $100 | $500 |
| CL | 0.20 × $1,000 | $200 |
| SI | 0.05 × $5,000 | $250 |
| NG | 0.030 × $10,000 | $300 |

---

## Risk Management Rules

### Prop Firm Compliance

The bot supports both Apex and Topstep challenge rules:

| Rule | Apex (50K) | Topstep (50K) |
|------|-----------|---------------|
| Max Trailing Drawdown | $2,500 (intraday unrealized) | $2,000 (end-of-day) |
| Daily Loss Limit | None | $1,000 |
| Profit Target | $3,000 | $3,000 |
| Max Contracts | 10 minis | 5 minis |
| Position Close By | 4:59 PM ET | 4:00 PM CT |

### Emergency Brake

The bot locks all trading when the daily P&L reaches **70%** of the daily loss limit:

- **Apex**: Locks when unrealized equity approaches 70% of the remaining distance to the drawdown floor.
- **Topstep**: Locks when day P&L hits -$700 (70% of $1,000 daily limit).

This provides a safety buffer to prevent accidental rule violations.

### Position Sizing

Each trade is sized dynamically:

```
Contracts = min(
    floor(Trade Risk Budget / Risk per Contract),
    Max Contracts - Open Contracts
)
```

Where:
- **Trade Risk Budget** = 2% of account size (configurable)
- **Risk per Contract** = Stop Loss (points) × Point Value per point

### All Orders Are Bracket Orders

Every entry order is automatically paired with:
- A **stop loss** order (Stop type)
- A **take profit** order (Limit type)

This is enforced at the API level using Tradovate's `placeOSO` endpoint. No "naked" market orders are ever sent.

---

## Trading Session Times

| Event | Time (ET) | Time (Israel) |
|-------|-----------|---------------|
| Market Open (ORB start) | 9:30 AM | 4:30 PM |
| ORB range closes | 9:35 AM | 4:35 PM |
| New trades cutoff | 3:30 PM | 10:30 PM |
| Force close (Apex) | 4:59 PM | 11:59 PM |
| Force close (Topstep) | 4:00 PM CT | 11:00 PM |

---

## Adjustment Notes

- All stop/profit values in `config.py` are configurable per symbol.
- To disable a symbol, set `"enabled": False` in `CONTRACT_SPECS`.
- The R/R ratio can be changed per symbol independently.
- For volatile days (FOMC, CPI, NFP), consider disabling the bot or widening stops manually.
- SI and NG are disabled by default due to high tick values and extreme volatility.
