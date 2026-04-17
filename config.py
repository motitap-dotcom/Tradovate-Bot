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
        "max_contracts": 9,               # FundedNext account cap = 9 micros total (user confirmed 2026-04-17)
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
# 0.50 = $500 on a $1000 limit → 2 full R-losses before halt.
# Matches the value running on the VPS as of the 2026-04-13 audit.
DAILY_LOSS_BRAKE_PCT = 0.50

# Hard cap: max total trades per day across all symbols (safety net).
# 6 symbols × ~4 trades/symbol = 24 upper bound under normal conditions.
MAX_DAILY_TRADES = 25

# Losing-streak kill-switch: after this many consecutive losing trades,
# pause trading for STREAK_PAUSE_MINUTES. Prevents tilting through a bad day.
MAX_LOSING_STREAK = 3
STREAK_PAUSE_MINUTES = 30

# Defensive sizing: when equity is closer than DEFENSIVE_FLOOR_DISTANCE
# to the trailing drawdown floor, cut position size in half.
DEFENSIVE_FLOOR_DISTANCE = 800.0

# Correlation guard: symbols that share directional exposure.
# The bot will not open a new position in a correlated symbol if one is
# already open in the same direction.
CORRELATED_GROUPS = [
    {"MNQ", "MES", "NQ", "ES"},   # equity indices
    {"MGC", "GC", "SIL", "SI"},   # precious metals
    {"MCL", "CL"},                # energy — oil
    {"MNG", "NG"},                # energy — nat gas
]

# ─────────────────────────────────────────────
# Contract Specifications
# ─────────────────────────────────────────────
CONTRACT_SPECS = {
    # ─── Micro Contracts (active) ──────────────────────────────
    # Switched from minis to micros for tighter risk control.
    # Point values are 1/10 of minis, allowing finer position sizing.
    "MNQ": {
        "name": "Micro E-mini Nasdaq-100",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 0.50,
        "point_value": 2.00,
        "strategy": "ORB",
        "enabled": True,
        "orb_windows": [5, 15],
        "max_orb_trades": 6,              # tightened from 15 — breakout should be rare
        "orb_cooldown_minutes": 20,
        "stop_loss_points": 20,
        "take_profit_points": 45,
        "risk_reward_ratio": 2.25,
        "min_range_points": 15,           # skip low-vol days
        "max_range_points": 150,          # NQ regularly opens 50-120pts
    },
    "MES": {
        "name": "Micro E-mini S&P 500",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 1.25,
        "point_value": 5.00,
        "strategy": "ORB",
        "enabled": True,
        "orb_windows": [5, 15],
        "max_orb_trades": 6,
        "orb_cooldown_minutes": 20,
        "stop_loss_points": 5,
        "take_profit_points": 10,
        "risk_reward_ratio": 2.0,
        "min_range_points": 3,
        "max_range_points": 40,           # ES regularly opens 10-25pts
    },
    "MGC": {
        "name": "Micro Gold (COMEX)",
        "exchange": "COMEX",
        "tick_size": 0.10,
        "tick_value": 1.00,
        "point_value": 10.00,
        "strategy": "VWAP",
        "enabled": True,
        "stop_loss_points": 4.0,
        "take_profit_points": 8.0,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 2,   # require 2 bars of cross confirmation
        "max_vwap_trades_per_direction": 4,
        "vwap_cooldown_minutes": 15,
        "min_trade_gap_minutes": 3,
        "max_vwap_distance_pct": 0.005,   # skip signal if price > 0.5% away from VWAP
    },
    "MCL": {
        "name": "Micro WTI Crude Oil",
        "exchange": "NYMEX",
        "tick_size": 0.01,
        "tick_value": 1.00,
        "point_value": 100.00,
        "strategy": "VWAP",
        "enabled": True,
        "stop_loss_points": 0.18,
        "take_profit_points": 0.36,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 2,
        "max_vwap_trades_per_direction": 4,
        "vwap_cooldown_minutes": 15,
        "min_trade_gap_minutes": 3,
        "max_vwap_distance_pct": 0.005,
    },
    "SIL": {
        "name": "Micro Silver (COMEX)",
        "exchange": "COMEX",
        "tick_size": 0.005,
        "tick_value": 1.00,               # 1,000 oz × $0.001 = $1 / $0.005 tick = $5? verify on VPS
        "point_value": 1_000.00,
        "strategy": "VWAP",
        "enabled": True,
        "stop_loss_points": 0.25,         # $250 risk per contract — sits inside $400 budget
        "take_profit_points": 0.50,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 2,
        "max_vwap_trades_per_direction": 3,
        "vwap_cooldown_minutes": 15,
        "min_trade_gap_minutes": 3,
        "max_vwap_distance_pct": 0.006,
    },
    "MNG": {
        "name": "Micro Henry Hub Natural Gas",
        "exchange": "NYMEX",
        "tick_size": 0.005,
        "tick_value": 1.25,
        "point_value": 250.00,
        "strategy": "VWAP",
        "enabled": True,
        "stop_loss_points": 0.060,        # $15 risk/contract — safe
        "take_profit_points": 0.120,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 2,
        "max_vwap_trades_per_direction": 3,
        "vwap_cooldown_minutes": 15,
        "min_trade_gap_minutes": 3,
        "max_vwap_distance_pct": 0.008,
    },
    # ─── Mini Contracts (disabled — switched to micros) ──────
    "NQ": {
        "name": "E-mini Nasdaq-100",
        "exchange": "CME",
        "tick_size": 0.25,
        "tick_value": 5.00,
        "point_value": 20.00,
        "strategy": "ORB",
        "enabled": False,
        "orb_windows": [5, 15],
        "max_orb_trades": 15,
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
        "enabled": False,
        "orb_windows": [5, 15],
        "max_orb_trades": 15,
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
        "enabled": False,
        "stop_loss_points": 5.0,
        "take_profit_points": 10.0,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 8,
        "vwap_cooldown_minutes": 30,
    },
    "CL": {
        "name": "WTI Crude Oil",
        "exchange": "NYMEX",
        "tick_size": 0.01,
        "tick_value": 10.00,
        "point_value": 1_000.00,
        "strategy": "VWAP",
        "enabled": False,
        "stop_loss_points": 0.20,
        "take_profit_points": 0.40,
        "risk_reward_ratio": 2.0,
        "vwap_confirmation_candles": 1,
        "max_vwap_trades_per_direction": 8,
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
        "max_vwap_trades_per_direction": 8,
        "vwap_cooldown_minutes": 30,
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
        "max_vwap_trades_per_direction": 8,
        "vwap_cooldown_minutes": 30,
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
    "SIL": ["H", "K", "N", "U", "Z"],
    # Natural Gas: every month
    "NG": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    "MNG": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
}

# Month code → month number mapping
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
MONTH_CODE_REVERSE = {v: k for k, v in MONTH_CODES.items()}


# ─────────────────────────────────────────────
# Front-month Contract Resolution
# ─────────────────────────────────────────────
# Tradovate's /contract/suggest API can return illiquid "serial" months
# (e.g. GCH6 — March gold, which is listed but carries almost no volume)
# and it does not always roll off a contract when First Notice Day passes.
# These helpers compute the correct front month locally from a calendar so
# the bot never ends up subscribed to an in-delivery or illiquid contract.

def _add_business_days(d, n):
    """Shift `d` by `n` business days (Mon–Fri). `n` may be negative."""
    from datetime import timedelta
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining > 0:
        d = d + timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


def _last_business_day(year, month):
    """Return the last business day (Mon–Fri) of a month."""
    import calendar
    from datetime import date, timedelta
    last = calendar.monthrange(year, month)[1]
    d = date(year, month, last)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _third_friday(year, month):
    """Return the third Friday of a month (used for index futures expiry)."""
    import calendar
    from datetime import date
    fridays = [date(year, month, day)
               for day in range(1, calendar.monthrange(year, month)[1] + 1)
               if date(year, month, day).weekday() == 4]
    return fridays[2]


def _contract_roll_out_date(base_symbol, year, month):
    """
    Return the last calendar date the bot should still be trading the
    (base_symbol, year, month) contract. After this date the bot must
    switch to the next liquid contract.

    Accounts for:
      • Cash-settled equity indices (NQ/ES/MNQ/MES/RTY/YM): third-Friday expiry
        of the contract month — roll 8 calendar days before expiry.
      • Physical-delivery metals and natural gas (GC/MGC/SI/SIL/PL/PA/NG/MNG):
        First Notice Day is the last business day of the month *prior* to
        the contract month — roll 3 business days before FND.
      • Crude oil (CL/MCL): last trading day is 3 business days before the
        25th of the month prior to the contract month — roll 5 business
        days before that.
    """
    from datetime import date, timedelta

    # Cash-settled equity index futures
    if base_symbol in ("NQ", "ES", "MNQ", "MES", "RTY", "M2K", "YM", "MYM"):
        return _third_friday(year, month) - timedelta(days=8)

    # Physical-delivery metals & natural gas — First Notice Day drives rollover
    if base_symbol in ("GC", "MGC", "SI", "SIL", "PL", "PA", "HG", "MHG",
                       "NG", "MNG"):
        prior_m = month - 1
        prior_y = year
        if prior_m == 0:
            prior_m = 12
            prior_y -= 1
        fnd = _last_business_day(prior_y, prior_m)
        return _add_business_days(fnd, -3)

    # Crude oil — last trading day is 3 biz days before 25th of prior month
    if base_symbol in ("CL", "MCL", "RB", "HO"):
        prior_m = month - 1
        prior_y = year
        if prior_m == 0:
            prior_m = 12
            prior_y -= 1
        ref = date(prior_y, prior_m, 25)
        while ref.weekday() >= 5:
            ref -= timedelta(days=1)
        ltd = _add_business_days(ref, -3)
        return _add_business_days(ltd, -5)

    # Fallback: roll on the last business day before the contract month begins.
    prior_m = month - 1
    prior_y = year
    if prior_m == 0:
        prior_m = 12
        prior_y -= 1
    return _last_business_day(prior_y, prior_m)


def get_front_month_contract(base_symbol, today=None):
    """
    Compute the correct front-month contract name for `base_symbol` on
    `today`, restricted to the product's liquid-month schedule and taking
    First Notice Day / expiry rules into account.

    Returns a contract name like 'GCM6' or None if the symbol is unknown.
    """
    from datetime import date
    if today is None:
        today = date.today()

    liquid = CONTRACT_LIQUID_MONTHS.get(base_symbol)
    if not liquid:
        return None

    # Look up to ~2 years forward. Iterating year-by-year preserves
    # chronological order within each year's liquid months.
    for y_offset in range(3):
        year = today.year + y_offset
        for mc in liquid:
            m = MONTH_CODES[mc]
            roll_out = _contract_roll_out_date(base_symbol, year, m)
            if today <= roll_out:
                return f"{base_symbol}{mc}{year % 10}"

    return None

# ─────────────────────────────────────────────
# Trading Session Times (Eastern Time)
# ─────────────────────────────────────────────
# US equity open for ORB calculation
MARKET_OPEN_ET = "09:30"

# Earliest time to place new trades (no trading before this)
TRADING_START_ET = "09:30"

# Stop placing new trades after this time
# Pulled in from 16:15 → 15:45 to avoid the chaotic closing-auction drift.
TRADING_CUTOFF_ET = "15:45"

# "No-fly zone" for ORB breakouts — midday chop window in ET.
# During this window ORB signals are suppressed (VWAP strategies still run).
ORB_BLACKOUT_START_ET = "10:30"
ORB_BLACKOUT_END_ET = "13:30"

# Force-close everything before this time
FORCE_CLOSE_ET = ACTIVE_CHALLENGE["close_by_et"]

# ─────────────────────────────────────────────
# Position Sizing
# ─────────────────────────────────────────────
# Max risk per trade as % of account.
# 0.8% of $50K = $400/trade → 2 clean R losses before the daily brake fires.
RISK_PER_TRADE_PCT = 0.008

# Hard ceiling on contracts per single trade, regardless of account size.
# Prevents a buggy budget calc from stacking dozens of contracts.
MAX_CONTRACTS_PER_TRADE = 5

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
