"""
Risk Manager
=============
Enforces prop firm challenge rules:
  - Trailing Max Drawdown (Apex: intraday unrealized, Topstep: EOD)
  - Daily Loss Limit (Topstep)
  - Emergency brake at configurable % of daily loss budget
  - Dynamic position sizing per contract based on tick value
  - Maximum contract limits
"""

import logging
import math
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


class RiskManager:
    """Tracks P&L and enforces all risk rules for the trading session."""

    def __init__(self):
        challenge = config.ACTIVE_CHALLENGE

        self.account_size: float = challenge["account_size"]
        self.max_trailing_drawdown: float = challenge["max_trailing_drawdown"]
        self.daily_loss_limit: Optional[float] = challenge["daily_loss_limit"]
        self.max_contracts: int = challenge["max_contracts"]
        self.trails_unrealized: bool = challenge["drawdown_trails_unrealized"]
        self.brake_pct: float = config.DAILY_LOSS_BRAKE_PCT
        self.daily_profit_cap: Optional[float] = challenge.get("daily_profit_cap")
        self.consistency_pct: Optional[float] = challenge.get("consistency_rule_pct")

        # Running state
        self.starting_balance: float = self.account_size
        self.current_balance: float = self.account_size
        self.peak_balance: float = self.account_size  # highest equity seen
        self.drawdown_floor: float = self.account_size - self.max_trailing_drawdown

        # Daily tracking (use ET timezone for day boundary)
        self.today: date = datetime.now(_ET).date()
        self.day_start_balance: float = self.account_size
        self.day_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self._balance_initialized: bool = False  # True once set_initial_balance succeeds

        # Lock flag
        self.trading_locked: bool = False
        self.lock_reason: str = ""

        # Open position tracking
        self.open_contracts: int = 0
        self._open_positions: list[dict] = []  # [{symbol, qty, stop_loss}, ...]

        # Daily trade counter (safety cap)
        self.max_daily_trades: int = config.MAX_DAILY_TRADES
        self.trades_today: int = 0

        logger.info(
            "RiskManager initialized | account=%s | max_dd=%s | daily_limit=%s | brake=%.0f%% | max_trades/day=%d | daily_profit_cap=%s",
            self.account_size,
            self.max_trailing_drawdown,
            self.daily_loss_limit,
            self.brake_pct * 100,
            self.max_daily_trades,
            f"${self.daily_profit_cap}" if self.daily_profit_cap else "None",
        )

    def set_initial_balance(self, balance: float):
        """Set the actual account balance from the API on startup.

        This corrects day_start_balance so that day_pnl is calculated
        relative to today's opening balance, not the original account_size.
        Can be called at startup or later (via _sync_balance fallback).
        """
        logger.info(
            "Setting initial balance from API: $%.2f (was $%.2f from config)",
            balance, self.day_start_balance,
        )
        self.current_balance = balance
        self.day_start_balance = balance
        self._balance_initialized = True
        # Peak/floor must also reflect reality
        if balance > self.peak_balance:
            self.peak_balance = balance
            self.drawdown_floor = self.peak_balance - self.max_trailing_drawdown
        elif balance < self.drawdown_floor + self.max_trailing_drawdown:
            # Balance is below where peak should be — set peak = balance
            # so drawdown floor is correct
            self.peak_balance = balance
            self.drawdown_floor = balance - self.max_trailing_drawdown
        logger.info(
            "Initial state: balance=$%.2f | peak=$%.2f | floor=$%.2f | day_start=$%.2f",
            self.current_balance, self.peak_balance, self.drawdown_floor, self.day_start_balance,
        )

    # ─────────────────────────────────────────
    # Balance updates
    # ─────────────────────────────────────────

    def update_balance(self, realized_balance: float, unrealized_pnl: float = 0.0):
        """Call this after every fill or on each tick to update risk state."""
        # Guard against corrupted data (NaN/Inf) — keep previous values
        if not math.isfinite(realized_balance) or not math.isfinite(unrealized_pnl):
            logger.error(
                "Invalid balance data (NaN/Inf): realized=%s unrealized=%s — skipping update",
                realized_balance, unrealized_pnl,
            )
            return

        self._check_new_day()

        self.current_balance = realized_balance
        self.unrealized_pnl = unrealized_pnl
        equity = realized_balance + unrealized_pnl

        # Update peak balance (Apex: intraday including unrealized)
        if self.trails_unrealized:
            if equity > self.peak_balance:
                self.peak_balance = equity
                self.drawdown_floor = self.peak_balance - self.max_trailing_drawdown
                logger.info(
                    "New peak equity: %.2f | drawdown floor: %.2f",
                    self.peak_balance,
                    self.drawdown_floor,
                )
        else:
            # Topstep: only trail on EOD realized balance (updated at end of day)
            pass

        # Daily P&L
        self.day_pnl = realized_balance - self.day_start_balance + unrealized_pnl

        # Check all rules
        self._check_drawdown(equity)
        self._check_daily_loss()
        self._check_daily_profit_cap()

    def end_of_day_update(self, realized_balance: float):
        """Call at session close for EOD trailing drawdown (Topstep)."""
        if not self.trails_unrealized:
            if realized_balance > self.peak_balance:
                self.peak_balance = realized_balance
                self.drawdown_floor = self.peak_balance - self.max_trailing_drawdown
                logger.info(
                    "EOD peak balance: %.2f | drawdown floor: %.2f",
                    self.peak_balance,
                    self.drawdown_floor,
                )

    def record_fill(self, pnl_change: float):
        """Record a realized P&L change from a filled order."""
        if not math.isfinite(pnl_change):
            logger.error("Invalid pnl_change (NaN/Inf): %s — skipping", pnl_change)
            return
        new_balance = self.current_balance + pnl_change
        if new_balance < 0:
            logger.error(
                "Balance would go negative: %.2f + %.2f = %.2f — clamping to 0",
                self.current_balance, pnl_change, new_balance,
            )
            new_balance = 0
        self.current_balance = new_balance
        self.update_balance(self.current_balance, self.unrealized_pnl)

    # ─────────────────────────────────────────
    # Rule checks
    # ─────────────────────────────────────────

    def _check_drawdown(self, equity: float):
        """Lock trading if equity breaches the trailing drawdown floor."""
        if equity <= self.drawdown_floor:
            self._lock(
                f"DRAWDOWN BREACH: equity {equity:.2f} <= floor {self.drawdown_floor:.2f}"
            )

    def _check_daily_loss(self):
        """Lock trading if approaching daily loss limit (emergency brake)."""
        if self.daily_loss_limit is None:
            return

        # Emergency brake at configured % of daily loss limit
        brake_threshold = -self.daily_loss_limit * self.brake_pct
        if self.day_pnl <= brake_threshold:
            self._lock(
                f"DAILY LOSS BRAKE: day P&L {self.day_pnl:.2f} hit "
                f"{self.brake_pct:.0%} of limit (-{self.daily_loss_limit:.2f})"
            )

    def _check_daily_profit_cap(self):
        """Lock trading if daily profit (including unrealized) exceeds cap (consistency rule)."""
        if self.daily_profit_cap is None:
            return
        # Use day_pnl which already includes unrealized P&L (set in update_balance)
        if self.day_pnl >= self.daily_profit_cap:
            self._lock(
                f"DAILY PROFIT CAP: day P&L ${self.day_pnl:.2f} hit "
                f"${self.daily_profit_cap:.2f} cap (consistency rule)"
            )

    def _check_new_day(self):
        """Reset daily counters if the date has changed (ET timezone)."""
        today = datetime.now(_ET).date()
        if today != self.today:
            logger.info("New trading day detected. Resetting daily state.")
            self.today = today
            self.day_start_balance = self.current_balance
            self.day_pnl = 0.0
            self.trades_today = 0
            self.trading_locked = False
            self.lock_reason = ""
            # Keep _balance_initialized — current_balance is already real
            # (it was set by API in the previous day's loop)

    def _lock(self, reason: str):
        """Lock all trading for the rest of the session."""
        if not self.trading_locked:
            self.trading_locked = True
            self.lock_reason = reason
            logger.critical("TRADING LOCKED: %s", reason)

    # ─────────────────────────────────────────
    # Pre-trade validation
    # ─────────────────────────────────────────

    def can_trade(self) -> tuple[bool, str]:
        """Check whether a new trade is allowed right now."""
        if self.trading_locked:
            return False, f"Trading locked: {self.lock_reason}"

        if self.open_contracts >= self.max_contracts:
            return False, (
                f"Max contracts reached: {self.open_contracts}/{self.max_contracts}"
            )

        if self.trades_today >= self.max_daily_trades:
            return False, (
                f"Daily trade cap reached: {self.trades_today}/{self.max_daily_trades}"
            )

        return True, "OK"

    def calculate_position_size(self, symbol: str) -> int:
        """
        Calculate how many contracts to trade for the given symbol,
        based on tick value and the per-trade risk budget.

        Includes aggregate risk check: total open stop-loss exposure
        plus new trade risk must not exceed daily loss budget.

        Returns 0 if trading is locked or risk budget is insufficient.
        """
        ok, reason = self.can_trade()
        if not ok:
            logger.warning("Position size = 0: %s", reason)
            return 0

        spec = config.CONTRACT_SPECS.get(symbol)
        if spec is None or not spec.get("enabled", False):
            return 0

        # Determine daily loss budget
        if self.daily_loss_limit is not None:
            daily_budget = self.daily_loss_limit
        else:
            # Apex: use remaining distance to drawdown floor as budget
            equity = self.current_balance + self.unrealized_pnl
            daily_budget = equity - self.drawdown_floor

        # Remaining budget = daily budget minus losses already taken today
        remaining_budget = daily_budget + self.day_pnl  # day_pnl is negative when losing

        # Subtract aggregate open risk (stop losses on existing positions)
        aggregate_open_risk = self._calculate_aggregate_open_risk()
        available_for_new_trade = remaining_budget - aggregate_open_risk

        if available_for_new_trade <= 0:
            logger.warning(
                "Position size = 0 for %s: remaining_budget=%.2f - open_risk=%.2f = %.2f (no room)",
                symbol, remaining_budget, aggregate_open_risk, available_for_new_trade,
            )
            return 0

        # Per-trade risk = configured % of account, capped at available budget
        trade_risk_budget = self.account_size * config.RISK_PER_TRADE_PCT
        trade_risk_budget = min(trade_risk_budget, available_for_new_trade)

        if trade_risk_budget <= 0:
            return 0

        # Dollar risk per contract = stop distance in points * point value
        stop_points = spec["stop_loss_points"]
        point_value = spec["point_value"]
        risk_per_contract = stop_points * point_value

        if risk_per_contract <= 0:
            return 0

        contracts = int(math.floor(trade_risk_budget / risk_per_contract))

        # Cap at available contract slots
        available = self.max_contracts - self.open_contracts
        contracts = min(contracts, available)

        # At least 1 if we have budget, at most max_contracts
        contracts = max(contracts, 0)

        logger.info(
            "Position size for %s: %d contracts | budget=%.2f | open_risk=%.2f | risk/contract=%.2f",
            symbol,
            contracts,
            trade_risk_budget,
            aggregate_open_risk,
            risk_per_contract,
        )
        return contracts

    def _calculate_aggregate_open_risk(self) -> float:
        """Calculate total dollar risk from all currently open positions.

        Uses the open_positions list tracked by the bot to sum up
        stop-loss exposure for each open trade.
        """
        total_risk = 0.0
        for pos in self._open_positions:
            spec = config.CONTRACT_SPECS.get(pos.get("symbol", ""))
            if spec:
                stop_points = spec["stop_loss_points"]
                point_value = spec["point_value"]
                qty = abs(pos.get("qty", 1))
                total_risk += stop_points * point_value * qty
        return total_risk

    def register_open(self, qty: int, symbol: str = ""):
        """Track that we opened positions."""
        self.open_contracts += qty
        self.trades_today += 1
        if symbol:
            self._open_positions.append({"symbol": symbol, "qty": qty})
            logger.info(
                "Registered open: %s x%d | aggregate_risk=$%.2f",
                symbol, qty, self._calculate_aggregate_open_risk(),
            )

    def register_close(self, qty: int, symbol: str = ""):
        """Track that we closed positions."""
        if qty > self.open_contracts:
            logger.warning(
                "register_close(%d) exceeds open_contracts(%d) — clamping to 0",
                qty, self.open_contracts,
            )
        self.open_contracts = max(0, self.open_contracts - qty)
        # Remove from open positions list
        if symbol:
            for i, pos in enumerate(self._open_positions):
                if pos.get("symbol") == symbol:
                    self._open_positions.pop(i)
                    break
        elif self._open_positions:
            # No symbol specified — remove first entry
            self._open_positions.pop(0)

    # ─────────────────────────────────────────
    # Status
    # ─────────────────────────────────────────

    def status(self) -> dict:
        """Return a snapshot of current risk state."""
        equity = self.current_balance + self.unrealized_pnl
        return {
            "balance": self.current_balance,
            "equity": equity,
            "day_pnl": self.day_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "peak_balance": self.peak_balance,
            "drawdown_floor": self.drawdown_floor,
            "distance_to_floor": equity - self.drawdown_floor,
            "daily_loss_limit": self.daily_loss_limit,
            "daily_loss_remaining": (
                (self.daily_loss_limit + self.day_pnl)
                if self.daily_loss_limit
                else None
            ),
            "daily_profit_cap": self.daily_profit_cap,
            "daily_profit_remaining": (
                (self.daily_profit_cap - self.day_pnl)
                if self.daily_profit_cap
                else None
            ),
            "open_contracts": self.open_contracts,
            "max_contracts": self.max_contracts,
            "trades_today": self.trades_today,
            "max_daily_trades": self.max_daily_trades,
            "locked": self.trading_locked,
            "lock_reason": self.lock_reason,
            "balance_initialized": self._balance_initialized,
        }
