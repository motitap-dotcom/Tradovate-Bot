"""
Trading Strategies
===================
ORB (Opening Range Breakout) — for equity index futures (NQ, ES).
VWAP Momentum — for commodity futures (GC, CL, SI, NG).

Each strategy class produces a Signal (Buy/Sell/None) with
stop-loss and take-profit prices, ready to be sent as a bracket order.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time
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
# ORB Strategy (5-minute Opening Range Breakout)
# ─────────────────────────────────────────────


class ORBStrategy:
    """
    Opening Range Breakout for NQ / ES.

    1. Record the high and low of the first N minutes after market open (9:30 ET).
    2. Enter long on a candle close above the range high.
    3. Enter short on a candle close below the range low.
    4. Stop loss at the opposite side of the range.
    5. Take profit at configured risk/reward ratio.
    6. Only one trade per day per symbol.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        spec = config.CONTRACT_SPECS[symbol]
        self.orb_minutes: int = spec["orb_window_minutes"]
        self.stop_points: float = spec["stop_loss_points"]
        self.tp_points: float = spec["take_profit_points"]
        self.rr_ratio: float = spec["risk_reward_ratio"]
        self.point_value: float = spec["point_value"]

        # State
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_set: bool = False
        self.trade_taken: bool = False
        self.prices_in_range: list[float] = []

        # Parse market open time
        h, m = config.MARKET_OPEN_ET.split(":")
        self.open_time = time(int(h), int(m))

    def reset(self):
        """Reset state for a new trading day."""
        self.range_high = None
        self.range_low = None
        self.range_set = False
        self.trade_taken = False
        self.prices_in_range = []

    def on_price(
        self, price: float, timestamp: datetime, high: float, low: float
    ) -> Optional[TradeSignal]:
        """
        Feed a price tick or candle.
        Returns a TradeSignal if a breakout is detected, else None.
        """
        if self.trade_taken:
            return None

        current_time = timestamp.time()

        # Phase 1: Building the opening range
        if not self.range_set:
            open_seconds = (
                self.open_time.hour * 3600 + self.open_time.minute * 60
            )
            current_seconds = (
                current_time.hour * 3600
                + current_time.minute * 60
                + current_time.second
            )
            elapsed_minutes = (current_seconds - open_seconds) / 60

            if 0 <= elapsed_minutes < self.orb_minutes:
                self.prices_in_range.append(high)
                self.prices_in_range.append(low)
                return None

            if elapsed_minutes >= self.orb_minutes and self.prices_in_range:
                self.range_high = max(self.prices_in_range)
                self.range_low = min(self.prices_in_range)
                self.range_set = True
                range_size = self.range_high - self.range_low
                logger.info(
                    "ORB %s range set: high=%.2f low=%.2f size=%.2f pts",
                    self.symbol,
                    self.range_high,
                    self.range_low,
                    range_size,
                )
                # Fall through to check breakout on this candle

        if not self.range_set:
            return None

        # Phase 2: Check for breakout
        range_size = self.range_high - self.range_low

        # Long breakout: candle closes above the range high
        if price > self.range_high:
            stop = self.range_low  # opposite side of range
            stop_distance = price - stop

            # Use configured stop if range is too wide
            if stop_distance > self.stop_points:
                stop = price - self.stop_points
                stop_distance = self.stop_points

            tp = price + (stop_distance * self.rr_ratio)
            self.trade_taken = True

            logger.info(
                "ORB %s LONG breakout at %.2f | SL=%.2f TP=%.2f",
                self.symbol,
                price,
                stop,
                tp,
            )
            return TradeSignal(
                symbol=self.symbol,
                direction=Direction.LONG,
                entry_price=None,  # market order
                stop_loss=stop,
                take_profit=tp,
                qty=0,  # will be set by risk manager
                reason=f"ORB long breakout above {self.range_high:.2f}",
            )

        # Short breakout: candle closes below the range low
        if price < self.range_low:
            stop = self.range_high
            stop_distance = stop - price

            if stop_distance > self.stop_points:
                stop = price + self.stop_points
                stop_distance = self.stop_points

            tp = price - (stop_distance * self.rr_ratio)
            self.trade_taken = True

            logger.info(
                "ORB %s SHORT breakout at %.2f | SL=%.2f TP=%.2f",
                self.symbol,
                price,
                stop,
                tp,
            )
            return TradeSignal(
                symbol=self.symbol,
                direction=Direction.SHORT,
                entry_price=None,
                stop_loss=stop,
                take_profit=tp,
                qty=0,
                reason=f"ORB short breakout below {self.range_low:.2f}",
            )

        return None


# ─────────────────────────────────────────────
# VWAP Momentum Strategy
# ─────────────────────────────────────────────


class VWAPStrategy:
    """
    VWAP Crossing / Momentum strategy for commodities (GC, CL, SI, NG).

    1. Calculate running VWAP from session start.
    2. Enter long when price crosses above VWAP with volume confirmation.
    3. Enter short when price crosses below VWAP.
    4. Stop loss just beyond VWAP.
    5. Take profit at configured risk/reward ratio.
    6. Maximum one trade per direction per day.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        spec = config.CONTRACT_SPECS[symbol]
        self.stop_points: float = spec["stop_loss_points"]
        self.tp_points: float = spec["take_profit_points"]
        self.rr_ratio: float = spec["risk_reward_ratio"]
        self.point_value: float = spec["point_value"]
        self.confirmation_candles: int = spec.get("vwap_confirmation_candles", 1)

        # VWAP calculation state
        self._cum_vol: float = 0.0
        self._cum_tp_vol: float = 0.0  # cumulative (typical_price * volume)
        self.vwap: Optional[float] = None

        # Crossover tracking
        self._prev_price: Optional[float] = None
        self._cross_above_count: int = 0
        self._cross_below_count: int = 0

        # Trade tracking
        self.long_taken: bool = False
        self.short_taken: bool = False

    def reset(self):
        """Reset for a new trading day."""
        self._cum_vol = 0.0
        self._cum_tp_vol = 0.0
        self.vwap = None
        self._prev_price = None
        self._cross_above_count = 0
        self._cross_below_count = 0
        self.long_taken = False
        self.short_taken = False

    def update_vwap(self, high: float, low: float, close: float, volume: float):
        """
        Update the running VWAP with a new bar.
        Call this for every candle/bar during the session.
        """
        if volume <= 0:
            return

        typical_price = (high + low + close) / 3.0
        self._cum_vol += volume
        self._cum_tp_vol += typical_price * volume

        if self._cum_vol > 0:
            self.vwap = self._cum_tp_vol / self._cum_vol

    def on_price(
        self, price: float, high: float, low: float, volume: float
    ) -> Optional[TradeSignal]:
        """
        Feed a candle close price + OHLCV data.
        Returns a TradeSignal if a confirmed VWAP crossover is detected.
        """
        # Update VWAP
        self.update_vwap(high, low, price, volume)

        if self.vwap is None or self._prev_price is None:
            self._prev_price = price
            return None

        signal = None

        # Detect crossover above VWAP
        if self._prev_price <= self.vwap and price > self.vwap:
            self._cross_above_count += 1
            self._cross_below_count = 0

            if (
                self._cross_above_count >= self.confirmation_candles
                and not self.long_taken
            ):
                stop = self.vwap - self.stop_points
                tp = price + self.tp_points
                self.long_taken = True

                logger.info(
                    "VWAP %s LONG cross at %.4f | VWAP=%.4f | SL=%.4f TP=%.4f",
                    self.symbol,
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
                    reason=f"VWAP long crossover at {price:.4f} (VWAP={self.vwap:.4f})",
                )

        # Detect crossover below VWAP
        elif self._prev_price >= self.vwap and price < self.vwap:
            self._cross_below_count += 1
            self._cross_above_count = 0

            if (
                self._cross_below_count >= self.confirmation_candles
                and not self.short_taken
            ):
                stop = self.vwap + self.stop_points
                tp = price - self.tp_points
                self.short_taken = True

                logger.info(
                    "VWAP %s SHORT cross at %.4f | VWAP=%.4f | SL=%.4f TP=%.4f",
                    self.symbol,
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
                    reason=f"VWAP short crossover at {price:.4f} (VWAP={self.vwap:.4f})",
                )
        else:
            # No crossover — reset counters
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
