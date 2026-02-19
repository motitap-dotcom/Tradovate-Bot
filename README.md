# Tradovate Trading Bot

Multi-asset futures trading bot built for Tradovate, designed for prop firm challenge accounts (Apex Trader Funding, Topstep).

## Features

- **Multi-asset support**: NQ, ES, GC, CL, SI, NG — each toggleable on/off
- **ORB strategy** (Opening Range Breakout) for equity index futures (NQ, ES)
- **VWAP Momentum strategy** for commodity futures (GC, CL, SI, NG)
- **Prop firm risk management**: Trailing Max Drawdown, Daily Loss Limit, emergency brake
- **All orders are bracket orders** (stop loss + take profit attached to every entry)
- **Automatic front-month contract detection** to avoid illiquid contracts
- **Dynamic position sizing** based on tick value and account risk budget
- **Force close** before session end to comply with prop firm rules

## Project Structure

```
Tradovate-Bot/
├── bot.py               # Main entry point — run this to start the bot
├── config.py            # All configuration (API, challenge rules, contracts)
├── risk_manager.py      # Risk management engine (drawdown, daily limits, sizing)
├── strategies.py        # ORB and VWAP strategy implementations
├── tradovate_api.py     # Tradovate REST + WebSocket API client
├── .env.example         # Template for API credentials
├── requirements.txt     # Python dependencies
├── Trading_Strategy.md  # Detailed strategy and risk documentation
└── README.md            # This file
```

## Setup — Step by Step

### 1. Prerequisites

- Python 3.10+
- A Tradovate account (demo or live)
- Tradovate API access enabled ($25/month subscription)
- CME market data subscription (for live market data via API)

### 2. Clone and Install

```bash
git clone https://github.com/motitap-dotcom/Tradovate-Bot.git
cd Tradovate-Bot
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure API Credentials

Copy the example env file and fill in your Tradovate credentials:

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
TRADOVATE_USERNAME=your_username
TRADOVATE_PASSWORD=your_password
TRADOVATE_APP_ID=your_app_id
TRADOVATE_CID=12345
TRADOVATE_SECRET=your_secret_key
TRADOVATE_DEVICE_ID=tradovate-bot-001
TRADOVATE_ENV=demo
PROP_FIRM=apex
```

**Where to find your API credentials:**
1. Log into Tradovate
2. Go to **Settings > API**
3. Click **Generate API key**
4. Save the `secret` (API secret) and `cid` (client ID / app ID)

### 4. Choose Your Prop Firm

In `.env`, set `PROP_FIRM` to either `apex` or `topstep`. This controls:
- Trailing drawdown calculation method
- Daily loss limit enforcement
- Max contracts allowed
- Session close time

### 5. Enable/Disable Instruments

In `config.py`, each instrument has an `"enabled": True/False` flag:

```python
CONTRACT_SPECS = {
    "NQ": { ..., "enabled": True },    # E-mini Nasdaq
    "ES": { ..., "enabled": True },    # E-mini S&P 500
    "GC": { ..., "enabled": True },    # Gold
    "CL": { ..., "enabled": True },    # Crude Oil
    "SI": { ..., "enabled": False },   # Silver (disabled — high tick value)
    "NG": { ..., "enabled": False },   # Natural Gas (disabled — very volatile)
}
```

Set `True` or `False` to control which instruments the bot trades.

### 6. Adjust Stop/Profit Levels

Each instrument in `config.py` has configurable stop and take profit parameters:

```python
"NQ": {
    "stop_loss_points": 25,      # Stop distance in points
    "take_profit_points": 50,    # Target distance in points
    "risk_reward_ratio": 2.0,    # R/R ratio
}
```

See `Trading_Strategy.md` for recommended values and reasoning.

## Running the Bot

### Demo Mode (Default)

```bash
python bot.py
```

Connects to Tradovate's demo environment. Real orders are placed on the demo account.

### Dry Run Mode

```bash
python bot.py --dry-run
```

No connection to Tradovate. Generates signals and logs them without placing any orders. Use this to verify strategy behavior.

### Live Mode

```bash
python bot.py --live
```

**Use with extreme caution.** This places real orders with real money.

## Monitoring Performance

### Log File

The bot writes to `bot.log` in the project directory. Every 30 seconds it logs a status line:

```
Status | balance=50000.00 | day_pnl=-150.00 | to_floor=2350.00 | contracts=1/10 | locked=False
```

Fields:
- `balance`: Current realized account balance
- `day_pnl`: Today's P&L (realized + unrealized)
- `to_floor`: Distance from current equity to the drawdown floor
- `contracts`: Open contracts vs. maximum allowed
- `locked`: Whether trading has been emergency-stopped

### End-of-Day Summary

When the bot shuts down (at force-close time or via Ctrl+C), it prints a summary of all trades taken during the session.

### Trade Signals

Every signal and order is logged with full details:

```
SIGNAL: Buy NQ 1 @ market | SL=15050.00 TP=15150.00 | ORB long breakout above 15075.00
```

## Safety Features

1. **Every order is a bracket order** — stop loss and take profit are always attached. No naked positions.
2. **Emergency brake** — trading locks at 70% of the daily loss limit (configurable via `DAILY_LOSS_BRAKE_PCT`).
3. **Force close** — all positions are flattened and orders cancelled before the prop firm's session cutoff time.
4. **Contract limit enforcement** — the bot refuses to open new positions beyond the maximum contract count.
5. **Trailing drawdown tracking** — peak equity is tracked tick-by-tick (Apex) or end-of-day (Topstep).
6. **Graceful shutdown** — Ctrl+C or SIGTERM triggers position close and order cancellation before exit.

## Tradovate API Notes

- The bot uses the Tradovate REST API v1 for authentication and order placement
- Market data is received via the Tradovate WebSocket market data stream
- All automated orders include `"isAutomated": true` as required by CME regulations
- Token renewal happens automatically when the access token is within 5 minutes of expiry
- Bracket orders use the `placeOSO` endpoint (entry + stop + target in one atomic call)

## References

- [Tradovate API Documentation](https://api.tradovate.com/)
- [Tradovate API FAQ (GitHub)](https://github.com/tradovate/example-api-faq)
- [Apex Trader Funding Rules](https://support.apextraderfunding.com/hc/en-us/articles/31519769997083-Evaluation-Rules)
- [Topstep Combine Rules](https://help.topstep.com/en/articles/8284204-what-is-the-maximum-loss-limit)
