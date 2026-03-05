"""
Tradovate Bot Configuration
============================
All settings for the multi-asset trading bot.
Put your API credentials in a .env file (never commit it).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# Tradovate API Credentials (from .env file)
# ─────────────────────────────────────────────
TRADOVATE_USERNAME = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID = os.getenv("TRADOVATE_APP_ID", "")
TRADOVATE_CID = int(os.getenv("TRADOVATE_CID", "0"))
TRADOVATE_SECRET = os.getenv("TRADOVATE_SECRET", "")
TRADOVATE_DEVICE_ID = os.getenv("TRADOVATE_DEVICE_ID", "tradovate-bot-001")

# Manual token override — paste from browser DevTools to skip CAPTCHA.
# After first use, the bot saves the token to .tradovate_token.json
# and renews it automatically. You only need this once.
TRADOVATE_ACCESS_TOKEN = os.getenv("TRADOVATE_ACCESS_TOKEN", "")

# ─────────────────────────────────────────────
# Environment: "demo" or "live"
# ─────────────────────────────────────────────
ENVIRONMENT = os.getenv("TRADOVATE_ENV", "demo")

_URLS = {
    "demo": {
        "rest": "https://demo.tradovateapi.com/v1",
        "ws_trading": "wss://demo.tradovateapi.com/v1/websocket",
        "ws_market": "wss://md-demo.tradovateapi.com/v1/websocket",
    },
    "live": {
        "rest": "https://live.tradovateapi.com/v1",
        "ws_trading": "wss://live.tradovateapi.com/v1/websocket",
        "ws_market": "wss://md.tradovateapi.com/v1/websocket",
    },
}

REST_URL = _URLS[ENVIRONMENT]["rest"]
WS_TRADING_URL = _URLS[ENVIRONMENT]["ws_trading"]
WS_MARKET_URL = _URLS[ENVIRONMENT]["ws_market"]

# ─────────────────────────────────────────────
# Prop Firm Challenge Settings
# ─────────────────────────────────────────────
PROP_FIRM = os.getenv("PROP_FIRM", "fundednext")  # "apex", "topstep", or "fundednext"

# Tradovate organization name (required for prop firm accounts)
# Each prop firm has its own org name that must be sent with auth requests.
TRADOVATE_ORGANIZATION = os.getenv("TRADOVATE_ORGANIZATION", "")

CHALLENGE_SETTINGS = {
    "apex": {
        "account_size": 50_000,
        "max_trailing_drawdown": 2_500,
        "daily_loss_limit": None,         # Apex has no daily loss limit
        "profit_target": 3_000,
        "max_contracts": 10,              # minis
        "close_by_et": "16:59",           # 4:59 PM ET
        "drawdown_trails_unrealized": True,  # Apex trails intraday unrealized peaks
        "organization": "",               # Tradovate org name
    },
    "topstep": {
        "account_size": 50_000,
        "max_trailing_drawdown": 2_000,
        "daily_loss_limit": 1_000,        # Topstep enforces per-day limit
        "profit_target": 3_000,
        "max_contracts": 5,
        "close_by_et": "15:00",           # 4:00 PM CT = 3:00 PM CT for cutoff
        "drawdown_trails_unrealized": False,  # Topstep trails EOD balance only
        "organization": "",
    },
    "fundednext": {
        "account_size": 50_000,
        "max_trailing_drawdown": 2_500,
        "daily_loss_limit": 1_000,        # FundedNext Futures daily limit (actual)
        "profit_target": 3_000,           # FundedNext Futures challenge target (actual)
        "max_contracts": 10,
        "close_by_et": "16:59",           # 4:59 PM ET
        "drawdown_trails_unrealized": True,
        "organization": "",               # FundedNext uses empty string (NOT "funded-next")
        "consistency_rule_pct": 0.40,     # Max single-day profit = 40% of total profit
        "daily_profit_cap": 2_400,        # Just under current highest day ($2,426)
    },
}

# Override organization from env if set, otherwise use the prop firm default
if not TRADOVATE_ORGANIZATION:
    TRADOVATE_ORGANIZATION = CHALLENGE_SETTINGS.get(PROP_FIRM, {}).get("organization", "")

ACTIVE_CHALLENGE = CHALLENGE_SETTINGS[PROP_FIRM]

# Emergency brake: stop trading at this % of the daily loss limit
# Lowered from 70% to 60% to compensate for increased trade frequency
DAILY_LOSS_BRAKE_PCT = 0.60  # 60% — tighter brake for higher frequency

# Hard cap: max total trades per day across all symbols (safety net)
MAX_DAILY_TRADES = 12

# ─────────────────────────────────────────────
# Contract Specifications
# ─────────────────────────────────────────────
CONTRACT_SPECS = {
    "NQ": {
        "name": "E-mini Nasdaq-100",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 5.00,
        "point_value": 20.00,
        "strategy": "ORB",
        "enabled": True,
        # Dual ORB windows: 5-min (aggressive) + 15-min (conservative, stronger signal)
        "orb_windows": [5, 15],
        "max_orb_trades": 2,            # max trades across all ORB windows
        "orb_cooldown_minutes": 15,     # min time between ORB trades
        "stop_loss_points": 25,
        "take_profit_points": 50,
        "risk_reward_ratio": 2.0,
    },
    "ES": {
        "name": "E-mini S&P 500",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 12.50,
        "point_value": 50.00,
        "strategy": "ORB",
        "enabled": True,
        "orb_windows": [5, 15],
        "max_orb_trades": 2,
        "orb_cooldown_minutes": 15,
        "stop_loss_points": 6,
        "take_profit_points": 12,
        "risk_reward_ratio": 2.0,
    },
    "GC": {
        "name": "Gold (COMEX)",
        "exchange": "COMEX",
        "tick_size": 0.10,
        "tick_value": 10.00,
        "point_value": 100.00,
        "strategy": "VWAP",
        "enabled": True,
        # VWAP / momentum params
        "stop_loss_points": 5.0,
        "take_profit_points": 10.0,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 2,  # allow 2 longs + 2 shorts per day
        "vwap_cooldown_minutes": 30,         # min 30 min between same-direction trades
    },
    "CL": {
        "name": "WTI Crude Oil",
        "exchange": "NYMEX",
        "tick_size": 0.01,
        "tick_value": 10.00,
        "point_value": 1_000.00,
        "strategy": "VWAP",
        "enabled": True,
        "stop_loss_points": 0.20,
        "take_profit_points": 0.40,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 2,
        "vwap_cooldown_minutes": 30,
    },
    "SI": {
        "name": "Silver (COMEX)",
        "exchange": "COMEX",
        "tick_size": 0.005,
        "tick_value": 25.00,
        "point_value": 5_000.00,
        "strategy": "VWAP",
        "enabled": False,  # Disabled by default — high tick value ($25)
        "stop_loss_points": 0.05,
        "take_profit_points": 0.10,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 1,  # conservative: 1 per direction
        "vwap_cooldown_minutes": 60,
    },
    "NG": {
        "name": "Henry Hub Natural Gas",
        "exchange": "NYMEX",
        "tick_size": 0.001,
        "tick_value": 10.00,
        "point_value": 10_000.00,
        "strategy": "VWAP",
        "enabled": False,  # Disabled by default — extremely volatile
        "stop_loss_points": 0.030,
        "take_profit_points": 0.060,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 1,
        "vwap_cooldown_minutes": 60,
    },
}

# ─────────────────────────────────────────────
# Trading Session Times (Eastern Time)
# ─────────────────────────────────────────────
# US equity open for ORB calculation
MARKET_OPEN_ET = "09:30"

# Earliest time to place new trades (no trading before this)
TRADING_START_ET = "09:30"

# Stop placing new trades after this time
TRADING_CUTOFF_ET = "15:30"

# Force-close everything before this time
FORCE_CLOSE_ET = ACTIVE_CHALLENGE["close_by_et"]

# ─────────────────────────────────────────────
# Position Sizing
# ─────────────────────────────────────────────
# Max risk per trade as % of daily loss budget
# Lowered from 2% to 1.5% to compensate for increased trade frequency
RISK_PER_TRADE_PCT = 0.015  # 1.5% of account per trade

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
