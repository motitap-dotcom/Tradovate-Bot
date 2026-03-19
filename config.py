"""
Tradovate Bot Configuration
============================
All settings for the multi-asset trading bot (v2.4 — mini contracts).
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
try:
    TRADOVATE_CID = int(os.getenv("TRADOVATE_CID", "0"))
except (ValueError, TypeError):
    TRADOVATE_CID = 0
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

if ENVIRONMENT not in _URLS:
    import logging as _log
    _log.getLogger(__name__).warning("Unknown ENVIRONMENT '%s', defaulting to 'demo'", ENVIRONMENT)
    ENVIRONMENT = "demo"

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
        "profit_target": 12_359,          # Consistency-adjusted: $4,943.36 highest day / 40% = $12,358.40
        "max_contracts": 10,              # micros (switched from minis for tighter risk)
        "close_by_et": "16:59",           # 4:59 PM ET
        "drawdown_trails_unrealized": True,
        "organization": "",               # FundedNext uses empty string (NOT "funded-next")
        "consistency_rule_pct": 0.40,     # Max single-day profit = 40% of total profit
        "consistency_rule": 0.40,         # Alias — used by target calculation
        "daily_profit_cap": 2_400,        # Keep below highest day ($4,943) to improve consistency ratio
    },
}

# Override organization from env if set, otherwise use the prop firm default
if not TRADOVATE_ORGANIZATION:
    TRADOVATE_ORGANIZATION = CHALLENGE_SETTINGS.get(PROP_FIRM, {}).get("organization", "")

if PROP_FIRM not in CHALLENGE_SETTINGS:
    import logging as _log
    _log.getLogger(__name__).warning("Unknown PROP_FIRM '%s', defaulting to 'fundednext'", PROP_FIRM)
    PROP_FIRM = "fundednext"

ACTIVE_CHALLENGE = CHALLENGE_SETTINGS[PROP_FIRM]

# Emergency brake: stop trading at this % of the daily loss limit
# Lowered from 70% to 60% to compensate for increased trade frequency
DAILY_LOSS_BRAKE_PCT = 0.60  # 60% — tighter brake for higher frequency

# Hard cap: max total trades per day across all symbols (safety net)
MAX_DAILY_TRADES = 16

# ─────────────────────────────────────────────
# Contract Specifications
# ─────────────────────────────────────────────
CONTRACT_SPECS = {
    # ─── Micro Contracts (disabled — FundedNext rejects micros) ─
    "MNQ": {
        "name": "Micro E-mini Nasdaq-100",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 0.50,
        "point_value": 2.00,
        "strategy": "ORB",
        "enabled": False,
        "orb_windows": [5, 15],
        "max_orb_trades": 2,
        "orb_cooldown_minutes": 15,
        "stop_loss_points": 25,
        "take_profit_points": 50,
        "risk_reward_ratio": 2.0,
    },
    "MES": {
        "name": "Micro E-mini S&P 500",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 1.25,
        "point_value": 5.00,
        "strategy": "ORB",
        "enabled": False,
        "orb_windows": [5, 15],
        "max_orb_trades": 2,
        "orb_cooldown_minutes": 15,
        "stop_loss_points": 6,
        "take_profit_points": 12,
        "risk_reward_ratio": 2.0,
    },
    "MGC": {
        "name": "Micro Gold (COMEX)",
        "exchange": "COMEX",
        "tick_size": 0.10,
        "tick_value": 1.00,
        "point_value": 10.00,
        "strategy": "VWAP",
        "enabled": False,
        "stop_loss_points": 5.0,
        "take_profit_points": 10.0,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 2,
        "vwap_cooldown_minutes": 30,
    },
    "MCL": {
        "name": "Micro WTI Crude Oil",
        "exchange": "NYMEX",
        "tick_size": 0.01,
        "tick_value": 1.00,
        "point_value": 100.00,
        "strategy": "VWAP",
        "enabled": False,
        "stop_loss_points": 0.20,
        "take_profit_points": 0.40,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 2,
        "vwap_cooldown_minutes": 30,
    },
    # ─── Mini Contracts (active — FundedNext requires minis) ──
    "NQ": {
        "name": "E-mini Nasdaq-100",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 5.00,
        "point_value": 20.00,
        "strategy": "ORB",
        "enabled": True,
        "orb_windows": [5, 15],
        "max_orb_trades": 2,
        "orb_cooldown_minutes": 15,
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
        "stop_loss_points": 5.0,
        "take_profit_points": 10.0,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 2,
        "vwap_cooldown_minutes": 30,
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
        "enabled": False,
        "stop_loss_points": 0.05,
        "take_profit_points": 0.10,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 1,
        "vwap_cooldown_minutes": 60,
    },
    "NG": {
        "name": "Henry Hub Natural Gas",
        "exchange": "NYMEX",
        "tick_size": 0.001,
        "tick_value": 10.00,
        "point_value": 10_000.00,
        "strategy": "VWAP",
        "enabled": False,
        "stop_loss_points": 0.030,
        "take_profit_points": 0.060,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 1,
        "vwap_cooldown_minutes": 60,
    },
}

# ─────────────────────────────────────────────
# Contract Rollover Schedule
# ─────────────────────────────────────────────
# How many calendar days before expiration to roll to the next contract.
# Tradovate's suggest API often lags, so we roll proactively.
ROLLOVER_DAYS_BEFORE_EXPIRY = 8

# Liquid contract months per product family.
# CME futures use month codes: F=Jan, G=Feb, H=Mar, J=Apr, K=May, M=Jun,
#                               N=Jul, Q=Aug, U=Sep, V=Oct, X=Nov, Z=Dec
# Only months listed here are considered for rollover.
CONTRACT_LIQUID_MONTHS = {
    # Equity indices: quarterly (H=Mar, M=Jun, U=Sep, Z=Dec)
    "NQ": ["H", "M", "U", "Z"],
    "ES": ["H", "M", "U", "Z"],
    "MNQ": ["H", "M", "U", "Z"],
    "MES": ["H", "M", "U", "Z"],
    # Gold: even months (G=Feb, J=Apr, M=Jun, Q=Aug, V=Oct, Z=Dec)
    "GC": ["G", "J", "M", "Q", "V", "Z"],
    "MGC": ["G", "J", "M", "Q", "V", "Z"],
    # Crude Oil: every month
    "CL": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    "MCL": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    # Silver: quarterly-ish (H=Mar, K=May, N=Jul, U=Sep, Z=Dec)
    "SI": ["H", "K", "N", "U", "Z"],
    # Natural Gas: every month
    "NG": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
}

# Month code → month number mapping
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
MONTH_CODE_REVERSE = {v: k for k, v in MONTH_CODES.items()}

# ─────────────────────────────────────────────
# Trading Session Times (Eastern Time)
# ─────────────────────────────────────────────
# US equity open for ORB calculation
MARKET_OPEN_ET = "09:30"

# Earliest time to place new trades (no trading before this)
TRADING_START_ET = "09:30"

# Stop placing new trades after this time
TRADING_CUTOFF_ET = "16:15"

# Force-close everything before this time
FORCE_CLOSE_ET = ACTIVE_CHALLENGE["close_by_et"]

# ─────────────────────────────────────────────
# Position Sizing
# ─────────────────────────────────────────────
# Max risk per trade as % of daily loss budget
# Lowered to 1.0% — tighter risk per trade, more trades allowed
RISK_PER_TRADE_PCT = 0.010  # 1.0% of account per trade

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
