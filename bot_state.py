"""
Bot State Persistence
======================
Saves and loads daily trading state between bot restarts.
Prevents duplicate trades after restart by remembering:
  - ORB breakout flags per symbol/window
  - VWAP trade counts and cooldown times per symbol
  - Daily trade count
  - Date (auto-resets on new day)
"""

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")


def save_state(state: dict):
    """Save bot state to disk."""
    state["_saved_at"] = datetime.now(timezone.utc).isoformat()
    state["_date"] = date.today().isoformat()
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error("Failed to save bot state: %s", e)


def load_state() -> Optional[dict]:
    """
    Load bot state from disk. Returns None if:
      - File doesn't exist
      - File is corrupt
      - State is from a different date (stale)
    """
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        # Only return state from today
        if state.get("_date") != date.today().isoformat():
            logger.info("Bot state is from %s (not today). Starting fresh.", state.get("_date"))
            return None
        return state
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Failed to load bot state: %s", e)
        return None


def build_state(
    strategies: dict,
    trades_today_count: int,
    trades_today_list: list,
    day_start_balance: Optional[float] = None,
) -> dict:
    """
    Build a state dict from current strategy instances.
    Called after every trade to persist state.
    """
    state = {
        "trades_today_count": trades_today_count,
        "symbols": {},
    }

    # Persist day_start_balance so mid-day restarts don't lose earlier P&L
    if day_start_balance is not None:
        state["day_start_balance"] = day_start_balance

    for symbol, strategy in strategies.items():
        sym_state = {"type": type(strategy).__name__}

        if hasattr(strategy, "windows"):
            # ORB strategy
            sym_state["trades_taken"] = strategy.trades_taken
            sym_state["last_trade_time"] = (
                strategy.last_trade_time.isoformat()
                if strategy.last_trade_time else None
            )
            sym_state["windows"] = []
            for w in strategy.windows:
                sym_state["windows"].append({
                    "window_minutes": w.window_minutes,
                    "range_set": w.range_set,
                    "range_high": w.range_high,
                    "range_low": w.range_low,
                    "breakout_fired": w.breakout_fired,
                })

        elif hasattr(strategy, "vwap"):
            # VWAP strategy
            sym_state["long_count"] = strategy.long_count
            sym_state["short_count"] = strategy.short_count
            sym_state["last_long_time"] = (
                strategy.last_long_time.isoformat()
                if strategy.last_long_time else None
            )
            sym_state["last_short_time"] = (
                strategy.last_short_time.isoformat()
                if strategy.last_short_time else None
            )
            sym_state["last_any_trade_time"] = (
                strategy.last_any_trade_time.isoformat()
                if strategy.last_any_trade_time else None
            )

        state["symbols"][symbol] = sym_state

    return state


def restore_strategies(state: dict, strategies: dict):
    """
    Restore strategy state from persisted state.
    Only restores trade counts and cooldowns — not VWAP/ORB range data
    (those are rebuilt from warmup).
    """
    if not state:
        return

    symbols_state = state.get("symbols", {})

    for symbol, strategy in strategies.items():
        sym_state = symbols_state.get(symbol)
        if not sym_state:
            continue

        if hasattr(strategy, "windows") and sym_state.get("type") == "ORBStrategy":
            # Restore ORB trade counts and breakout flags
            strategy.trades_taken = sym_state.get("trades_taken", 0)
            if sym_state.get("last_trade_time"):
                try:
                    strategy.last_trade_time = datetime.fromisoformat(sym_state["last_trade_time"])
                except (ValueError, TypeError):
                    pass

            # Restore breakout_fired flags per window
            saved_windows = sym_state.get("windows", [])
            for i, w in enumerate(strategy.windows):
                if i < len(saved_windows):
                    sw = saved_windows[i]
                    if sw.get("breakout_fired"):
                        w.breakout_fired = True
                    # Restore range if warmup didn't set it
                    if not w.range_set and sw.get("range_set"):
                        w.range_high = sw["range_high"]
                        w.range_low = sw["range_low"]
                        w.range_set = True

            logger.info(
                "Restored ORB state for %s: trades_taken=%d, windows=%s",
                symbol, strategy.trades_taken,
                [w.breakout_fired for w in strategy.windows],
            )

        elif hasattr(strategy, "vwap") and sym_state.get("type") == "VWAPStrategy":
            # Restore VWAP trade counts and cooldowns
            strategy.long_count = sym_state.get("long_count", 0)
            strategy.short_count = sym_state.get("short_count", 0)

            for attr in ("last_long_time", "last_short_time", "last_any_trade_time"):
                val = sym_state.get(attr)
                if val:
                    try:
                        setattr(strategy, attr, datetime.fromisoformat(val))
                    except (ValueError, TypeError):
                        pass

            logger.info(
                "Restored VWAP state for %s: longs=%d, shorts=%d",
                symbol, strategy.long_count, strategy.short_count,
            )
