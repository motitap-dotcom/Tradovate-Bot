"""
Trading Strategies
===================
ORB (Opening Range Breakout) — for equity index futures (NQ, ES).
  Now supports dual time windows (5-min + 15-min) for higher frequency.

VWAP Momentum — for commodity futures (GC, CL, SI, NG).
  Now supports multiple trades per direction with cooldown.

Each strategy class produces a Signal (Buy/Sell/None) with
stop-loss and take-profit prices, ready to be sent as a bracket order.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Optional

import numpy as np

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Signal dataclass
# ─────────────────────────────────────────────


class Direction(Enum):
    LONG = "Buy"
    SHORT = "Sell"


@dataclass
class TradeSignal:
    symbol: str
    direction: Direction
    entry_price: Optional[float]  # None = market order
    stop_loss: float
    take_profit: float
    qty: int
    reason: str


# ─────────────────────────────────────────────
# Single ORB Window (internal helper)
# ─────────────────────────────────────────────


class _ORBWindow:
    """Tracks one opening range window and detects breakouts."""

    # When the bot starts after the ORB window, build a range from this
    # many minutes of live data instead of waiting until tomorrow.
    LATE_START_WARMUP_MINUTES = 2

    def __init__(self, window_minutes: int, open_time: time):
        self.window_minutes = window_minutes
        self.open_time = open_time
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_set: bool = False
        self.breakout_fired: bool = False
        self.prices: list[float] = []
        self._last_price: Optional[float] = None
        self._late_start_seconds: Optional[int] = None  # tracks late-start warmup

    def reset(self):
        self.range_high = None
        self.range_low = None
        self.range_set = False
        self.breakout_fired = False
        self.prices = []
        self._last_price = None
        self._late_start_seconds = None

    def _try_set_range(self, label: str) -> bool:
        """Try to set the range from accumulated prices. Returns True if set."""
        if not self.prices:
            return False
        self.range_high = max(self.prices)
        self.range_low = min(self.prices)
        range_size = self.range_high - self.range_low
        if range_size <= 0:
            logger.warning(
                "ORB %d-min %s range invalid (size=%.2f). Skipping.",
                self.window_minutes, label, range_size,
            )
            return False
        self.range_set = True
        logger.info(
            "ORB %d-min %s range: high=%.2f low=%.2f size=%.2f",
            self.window_minutes,
            label,
            self.range_high,
            self.range_low,
            range_size,
        )
        return True

    def feed(self, price: float, high: float, low: float, current_time: time) -> Optional[str]:
        """
        Feed a price. Returns 'long', 'short', or None.
        Only fires once per window.

        Requires a fresh cross: the previous price must have been inside the
        range for a breakout to fire.  This prevents stale breakouts after a
        bot restart (warmup sets _last_price to the last historical close).
        """
        if self.breakout_fired:
            return None

        open_seconds = self.open_time.hour * 3600 + self.open_time.minute * 60
        current_seconds = (
            current_time.hour * 3600
            + current_time.minute * 60
            + current_time.second
        )
        elapsed = (current_seconds - open_seconds) / 60

        # Phase 1: Accumulate range
        if not self.range_set:
            if 0 <= elapsed < self.window_minutes:
                # Normal: within the ORB window, collect prices
                self.prices.append(high)
                self.prices.append(low)
                self._last_price = price
                return None

            if elapsed >= self.window_minutes and self.prices:
                # Normal: ORB window just ended, set range
                self._try_set_range("normal")
                # Fall through to check breakout

            elif elapsed >= self.window_minutes and not self.prices:
                # Late start: bot restarted after the ORB window.
                # Build a quick range from incoming tick prices.
                # NOTE: use `price` (current tick), NOT `high`/`low` which
                # are session extremes and would create an absurdly wide range.
                if self._late_start_seconds is None:
                    self._late_start_seconds = current_seconds
                    logger.info(
                        "ORB %d-min: late start (%.0fm after open), "
                        "building %d-min warmup range...",
                        self.window_minutes, elapsed,
                        self.LATE_START_WARMUP_MINUTES,
                    )

                self.prices.append(price)
                self._last_price = price

                warmup_elapsed = (current_seconds - self._late_start_seconds) / 60
                if warmup_elapsed >= self.LATE_START_WARMUP_MINUTES and len(self.prices) >= 10:
                    self._try_set_range("late-start")
                return None

        if not self.range_set:
            return None

        # Phase 2: Breakout detection — require fresh cross from inside range
        prev = self._last_price
        self._last_price = price

        if prev is not None and not (self.range_low <= prev <= self.range_high):
            # Previous price was outside the range — not a fresh cross
            return None

        if price > self.range_high:
            self.breakout_fired = True
            return "long"
        if price < self.range_low:
            self.breakout_fired = True
            return "short"

        return None


# ─────────────────────────────────────────────
# ORB Strategy — Dual Window
# ─────────────────────────────────────────────


class ORBStrategy:
    """
    Dual-window Opening Range Breakout for NQ / ES.

    Window 1 (5-min): Fast, aggressive breakout — fires first.
    Window 2 (15-min): Wider range, stronger confirmation — fires later.

    Each window can produce one breakout. Total trades capped at max_orb_trades.
    A cooldown period separates consecutive trades.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        spec = config.CONTRACT_SPECS.get(symbol)
        if spec is None:
            raise ValueError(f"No contract spec found for symbol: {symbol}")

        self.stop_points: float = spec["stop_loss_points"]
        self.tp_points: float = spec["take_profit_points"]
        self.rr_ratio: float = spec["risk_reward_ratio"]
        self.point_value: float = spec["point_value"]
        self.max_trades: int = spec.get("max_orb_trades", 2)
        self.cooldown_minutes: int = spec.get("orb_cooldown_minutes", 15)

        # Parse market open time
        h, m = config.MARKET_OPEN_ET.split(":")
        open_time = time(int(h), int(m))

        # Create one _ORBWindow per configured window size
        windows = spec.get("orb_windows", [5])
        self.windows: list[_ORBWindow] = [
            _ORBWindow(w, open_time) for w in windows
        ]

        # Trade state
        self.trades_taken: int = 0
        self.last_trade_time: Optional[datetime] = None

    def reset(self):
        """Reset state for a new trading day."""
        for w in self.windows:
            w.reset()
        self.trades_taken = 0
        self.last_trade_time = None

    def on_price(
        self, price: float, timestamp: datetime, high: float, low: float
    ) -> Optional[TradeSignal]:
        """
        Feed a price tick or candle.
        Returns a TradeSignal if a breakout is detected, else None.
        """
        if self.trades_taken >= self.max_trades:
            return None

        # Cooldown check
        if self.last_trade_time is not None:
            elapsed = (timestamp - self.last_trade_time).total_seconds() / 60
            if elapsed < self.cooldown_minutes:
                return None

        current_time = timestamp.time()

        # Try each window (shorter windows fire earlier)
        for window in self.windows:
            direction = window.feed(price, high, low, current_time)
            if direction is None:
                continue

            # Build signal
            spec = config.CONTRACT_SPECS[self.symbol]
            min_stop = spec["tick_size"] * 2  # minimum 2 ticks stop distance

            if direction == "long":
                stop = window.range_low
                stop_distance = price - stop
                if stop_distance <= 0:
                    # Price is at or below range low — invalid long breakout
                    logger.warning(
                        "ORB %dm long: stop_distance <= 0 (price=%.2f, range_low=%.2f). Skipping.",
                        window.window_minutes, price, window.range_low,
                    )
                    continue
                if stop_distance > self.stop_points:
                    stop = price - self.stop_points
                    stop_distance = self.stop_points
                if stop_distance < min_stop:
                    logger.info("ORB %s long skipped: stop distance %.4f < min %.4f",
                                self.symbol, stop_distance, min_stop)
                    continue
                tp = price + (stop_distance * self.rr_ratio)
                sig_dir = Direction.LONG
                reason = (
                    f"ORB-{window.window_minutes}m long breakout "
                    f"above {window.range_high:.2f}"
                )
            else:  # short
                stop = window.range_high
                stop_distance = stop - price
                if stop_distance <= 0:
                    # Price is at or above range high — invalid short breakout
                    logger.warning(
                        "ORB %dm short: stop_distance <= 0 (price=%.2f, range_high=%.2f). Skipping.",
                        window.window_minutes, price, window.range_high,
                    )
                    continue
                if stop_distance > self.stop_points:
                    stop = price + self.stop_points
                    stop_distance = self.stop_points
                if stop_distance < min_stop:
                    logger.info("ORB %s short skipped: stop distance %.4f < min %.4f",
                                self.symbol, stop_distance, min_stop)
                    continue
                tp = price - (stop_distance * self.rr_ratio)
                sig_dir = Direction.SHORT
                reason = (
                    f"ORB-{window.window_minutes}m short breakout "
                    f"below {window.range_low:.2f}"
                )

            self.trades_taken += 1
            self.last_trade_time = timestamp

            logger.info(
                "ORB %s %s at %.2f | window=%dm | trade %d/%d | SL=%.2f TP=%.2f",
                self.symbol,
                direction.upper(),
                price,
                window.window_minutes,
                self.trades_taken,
                self.max_trades,
                stop,
                tp,
            )
            return TradeSignal(
                symbol=self.symbol,
                direction=sig_dir,
                entry_price=None,
                stop_loss=stop,
                take_profit=tp,
                qty=0,  # set by risk manager
                reason=reason,
            )

        return None


# ─────────────────────────────────────────────
# VWAP Momentum Strategy — Multi-trade with cooldown
# ─────────────────────────────────────────────


class VWAPStrategy:
    """
    VWAP Crossing / Momentum strategy for commodities (GC, CL, SI, NG).

    Now supports multiple trades per direction with a cooldown period.
    This safely increases frequency: each subsequent crossover must be
    a fresh move separated by at least vwap_cooldown_minutes.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        spec = config.CONTRACT_SPECS.get(symbol)
        if spec is None:
            raise ValueError(f"No contract spec found for symbol: {symbol}")
        self.stop_points: float = spec["stop_loss_points"]
        self.tp_points: float = spec["take_profit_points"]
        self.rr_ratio: float = spec["risk_reward_ratio"]
        self.point_value: float = spec["point_value"]
        self.confirmation_candles: int = spec.get("vwap_confirmation_candles", 1)

        # Multi-trade settings
        self.max_per_direction: int = spec.get("max_vwap_trades_per_direction", 2)
        self.cooldown_minutes: int = spec.get("vwap_cooldown_minutes", 30)
        # Minimum time between ANY trades (regardless of direction) to prevent whipsaw
        self.min_trade_gap_minutes: int = 3

        # VWAP calculation state
        self._cum_vol: float = 0.0
        self._cum_tp_vol: float = 0.0
        self.vwap: Optional[float] = None

        # Crossover tracking
        self._prev_price: Optional[float] = None
        self._cross_above_count: int = 0
        self._cross_below_count: int = 0

        # Trade tracking
        self.long_count: int = 0
        self.short_count: int = 0
        self.last_long_time: Optional[datetime] = None
        self.last_short_time: Optional[datetime] = None
        self.last_any_trade_time: Optional[datetime] = None  # cross-direction cooldown
        self._current_time: Optional[datetime] = None  # set by bot on each tick

    def reset(self):
        """Reset for a new trading day."""
        self._cum_vol = 0.0
        self._cum_tp_vol = 0.0
        self.vwap = None
        self._prev_price = None
        self._cross_above_count = 0
        self._cross_below_count = 0
        self.long_count = 0
        self.short_count = 0
        self.last_long_time = None
        self.last_short_time = None
        self.last_any_trade_time = None
        self._candle_count = 0
        self._vwap_stale_bars = 0

    def update_vwap(self, high: float, low: float, close: float, volume: float):
        """Update the running VWAP with a new bar."""
        if volume <= 0:
            # Tick-level quotes from WebSocket never carry volume — this is
            # expected and NOT an indication of stale data.  Only candle bars
            # (from the REST poller) provide real volume.  Do NOT increment
            # _vwap_stale_bars here; the staleness guard is meant for detecting
            # when the entire data feed has died, not for individual ticks.
            return
        # Sanity-check OHLC: swap if reversed (data corruption guard)
        if high < low:
            high, low = low, high
        # Validate close is within high/low range (data corruption)
        if close > high:
            close = high
        elif close < low:
            close = low
        # Reject extreme outliers: if any price is <= 0 or NaN
        if not (high > 0 and low > 0 and close > 0):
            logger.warning("VWAP %s: invalid OHLC (h=%.4f l=%.4f c=%.4f). Skipping.", self.symbol, high, low, close)
            return
        typical_price = (high + low + close) / 3.0
        self._cum_vol += volume
        self._cum_tp_vol += typical_price * volume
        if self._cum_vol > 0:
            self.vwap = self._cum_tp_vol / self._cum_vol
        self._vwap_stale_bars = 0  # Reset staleness counter

    def _long_allowed(self) -> bool:
        """Check if a new long trade is allowed (count + cooldown)."""
        if self.long_count >= self.max_per_direction:
            return False
        if self.last_long_time and self._current_time:
            elapsed = (self._current_time - self.last_long_time).total_seconds() / 60
            if elapsed < self.cooldown_minutes:
                return False
        return True

    def _short_allowed(self) -> bool:
        """Check if a new short trade is allowed (count + cooldown)."""
        if self.short_count >= self.max_per_direction:
            return False
        if self.last_short_time and self._current_time:
            elapsed = (self._current_time - self.last_short_time).total_seconds() / 60
            if elapsed < self.cooldown_minutes:
                return False
        return True

    # Minimum candles of data before generating signals
    MIN_CANDLES_FOR_SIGNAL = 5

    def on_price(
        self, price: float, high: float, low: float, volume: float
    ) -> Optional[TradeSignal]:
        """
        Feed a candle close price + OHLCV data.
        Returns a TradeSignal if a confirmed VWAP crossover is detected.
        """
        self.update_vwap(high, low, price, volume)
        self._candle_count = getattr(self, "_candle_count", 0) + 1

        if self.vwap is None or self._prev_price is None:
            self._prev_price = price
            return None

        # Don't signal until we have enough data to compute a meaningful VWAP
        if self._candle_count < self.MIN_CANDLES_FOR_SIGNAL:
            self._prev_price = price
            return None

        # Don't signal if VWAP is stale (too many zero-volume bars)
        stale_bars = getattr(self, "_vwap_stale_bars", 0)
        if stale_bars >= 3:
            self._prev_price = price
            return None

        # Cross-direction cooldown: prevent whipsaw (e.g. SHORT then LONG in seconds)
        if self.last_any_trade_time and self._current_time:
            gap = (self._current_time - self.last_any_trade_time).total_seconds() / 60
            if gap < self.min_trade_gap_minutes:
                self._prev_price = price
                return None

        signal = None

        # Detect crossover above VWAP
        if self._prev_price <= self.vwap and price > self.vwap:
            self._cross_above_count += 1
            self._cross_below_count = 0

            if (
                self._cross_above_count >= self.confirmation_candles
                and self._long_allowed()
            ):
                stop = self.vwap - self.stop_points
                tp = price + self.tp_points
                self.long_count += 1
                self.last_long_time = self._current_time
                self.last_any_trade_time = self._current_time
                self._cross_above_count = 0  # reset for potential next trade

                logger.info(
                    "VWAP %s LONG #%d at %.4f | VWAP=%.4f | SL=%.4f TP=%.4f",
                    self.symbol,
                    self.long_count,
                    price,
                    self.vwap,
                    stop,
                    tp,
                )
                signal = TradeSignal(
                    symbol=self.symbol,
                    direction=Direction.LONG,
                    entry_price=None,
                    stop_loss=stop,
                    take_profit=tp,
                    qty=0,
                    reason=(
                        f"VWAP long #{self.long_count} at {price:.4f} "
                        f"(VWAP={self.vwap:.4f})"
                    ),
                )

        # Detect crossover below VWAP
        elif self._prev_price >= self.vwap and price < self.vwap:
            self._cross_below_count += 1
            self._cross_above_count = 0

            if (
                self._cross_below_count >= self.confirmation_candles
                and self._short_allowed()
            ):
                stop = self.vwap + self.stop_points
                tp = price - self.tp_points
                self.short_count += 1
                self.last_short_time = self._current_time
                self.last_any_trade_time = self._current_time
                self._cross_below_count = 0

                logger.info(
                    "VWAP %s SHORT #%d at %.4f | VWAP=%.4f | SL=%.4f TP=%.4f",
                    self.symbol,
                    self.short_count,
                    price,
                    self.vwap,
                    stop,
                    tp,
                )
                signal = TradeSignal(
                    symbol=self.symbol,
                    direction=Direction.SHORT,
                    entry_price=None,
                    stop_loss=stop,
                    take_profit=tp,
                    qty=0,
                    reason=(
                        f"VWAP short #{self.short_count} at {price:.4f} "
                        f"(VWAP={self.vwap:.4f})"
                    ),
                )
        else:
            if price > self.vwap:
                self._cross_below_count = 0
            else:
                self._cross_above_count = 0

        self._prev_price = price
        return signal


# ─────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────


def create_strategy(symbol: str):
    """Create the appropriate strategy instance for a symbol."""
    spec = config.CONTRACT_SPECS.get(symbol)
    if spec is None:
        raise ValueError(f"Unknown symbol: {symbol}")

    strategy_type = spec["strategy"]
    if strategy_type == "ORB":
        return ORBStrategy(symbol)
    elif strategy_type == "VWAP":
        return VWAPStrategy(symbol)
    else:
        raise ValueError(f"Unknown strategy type: {strategy_type}")
