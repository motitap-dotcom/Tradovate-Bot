"""
Risk Manager
=============
Enforces prop firm challenge rules:
  - Trailing Max Drawdown (Apex: intraday unrealized, Topstep: EOD)
  - Daily Loss Limit (Topstep)
  - Emergency brake at configurable % of daily loss budget
  - Dynamic position sizing per contract based on tick value
  - Maximum contract limits
  - Daily profit cap (consistency rule) — persists across restarts
"""

import json
import logging
import math
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import config

# Persist day-start balance so restarts don't reset daily P&L
_DAY_STATE_FILE = Path(__file__).parent / ".day_state.json"

logger = logging.getLogger(__name__)


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

        # Daily tracking
        self.today: date = date.today()
        self.day_start_balance: float = self.account_size
        self.day_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0

        # Lock flag
        self.trading_locked: bool = False
        self.lock_reason: str = ""

        # Open position tracking
        self.open_contracts: int = 0

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

    # ─────────────────────────────────────────
    # Persistent day-start balance (survives restarts)
    # ─────────────────────────────────────────

    def _save_day_state(self, date_str: str, day_start_balance: float):
        """Save today's start-of-day balance to disk."""
        try:
            _DAY_STATE_FILE.write_text(
                json.dumps({"date": date_str, "day_start_balance": day_start_balance})
            )
        except Exception as e:
            logger.warning("Failed to save day state: %s", e)

    def _load_day_state(self) -> Optional[dict]:
        """Load saved day-start state from disk."""
        try:
            if _DAY_STATE_FILE.exists():
                return json.loads(_DAY_STATE_FILE.read_text())
        except Exception as e:
            logger.warning("Failed to load day state: %s", e)
        return None

    def set_initial_balance(self, balance: float):
        """
        Set balance from API on startup, preserving daily P&L if the bot
        restarts mid-day.  This prevents the daily profit cap from being
        bypassed by a restart.
        """
        saved = self._load_day_state()
        today_str = date.today().isoformat()

        if saved and saved.get("date") == today_str:
            # ── Same-day restart: recover the real day-start balance ──
            self.day_start_balance = saved["day_start_balance"]
            self.day_pnl = balance - self.day_start_balance
            logger.info(
                "Mid-day restart detected | day_start=%.2f | current=%.2f | day_pnl=%.2f",
                self.day_start_balance, balance, self.day_pnl,
            )
            # Re-apply profit cap check immediately
            if self.daily_profit_cap and self.day_pnl >= self.daily_profit_cap:
                self._lock(
                    f"DAILY PROFIT CAP (resumed): day P&L ${self.day_pnl:.2f} "
                    f">= cap ${self.daily_profit_cap:.2f}"
                )
            # Re-apply daily loss check
            if self.daily_loss_limit:
                brake_threshold = -self.daily_loss_limit * self.brake_pct
                if self.day_pnl <= brake_threshold:
                    self._lock(
                        f"DAILY LOSS BRAKE (resumed): day P&L ${self.day_pnl:.2f}"
                    )
        else:
            # ── New trading day: record start-of-day balance ──
            self.day_start_balance = balance
            self.day_pnl = 0.0
            self.trading_locked = False
            self.lock_reason = ""
            self._save_day_state(today_str, balance)
            logger.info("New trading day | day_start=%.2f", balance)

        self.current_balance = balance
        self.peak_balance = max(balance, self.peak_balance)
        self.drawdown_floor = self.peak_balance - self.max_trailing_drawdown

    # ─────────────────────────────────────────
    # Balance updates
    # ─────────────────────────────────────────

    def update_balance(self, realized_balance: float, unrealized_pnl: float = 0.0):
        """Call this after every fill or on each tick to update risk state."""
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
        self.current_balance += pnl_change
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
        """Lock trading if daily profit exceeds cap (consistency rule)."""
        if self.daily_profit_cap is None:
            return
        if self.day_pnl >= self.daily_profit_cap:
            self._lock(
                f"DAILY PROFIT CAP: day P&L ${self.day_pnl:.2f} hit "
                f"${self.daily_profit_cap:.2f} cap (consistency rule)"
            )

    def _check_new_day(self):
        """Reset daily counters if the date has changed."""
        today = date.today()
        if today != self.today:
            logger.info("New trading day detected. Resetting daily state.")
            self.today = today
            self.day_start_balance = self.current_balance
            self.day_pnl = 0.0
            self.trades_today = 0
            self.trading_locked = False
            self.lock_reason = ""

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

        Returns 0 if trading is locked or risk budget is insufficient.
        """
        ok, reason = self.can_trade()
        if not ok:
            logger.warning("Position size = 0: %s", reason)
            return 0

        spec = config.CONTRACT_SPECS.get(symbol)
        if spec is None or not spec["enabled"]:
            return 0

        # Determine daily loss budget
        if self.daily_loss_limit is not None:
            daily_budget = self.daily_loss_limit
        else:
            # Apex: use remaining distance to drawdown floor as budget
            equity = self.current_balance + self.unrealized_pnl
            daily_budget = equity - self.drawdown_floor

        # Per-trade risk = configured % of account
        trade_risk_budget = self.account_size * config.RISK_PER_TRADE_PCT

        # Don't risk more than remaining daily budget
        trade_risk_budget = min(trade_risk_budget, daily_budget + self.day_pnl)

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
            "Position size for %s: %d contracts | budget=%.2f | risk/contract=%.2f",
            symbol,
            contracts,
            trade_risk_budget,
            risk_per_contract,
        )
        return contracts

    def register_open(self, qty: int):
        """Track that we opened positions."""
        self.open_contracts += qty
        self.trades_today += 1

    def register_close(self, qty: int):
        """Track that we closed positions."""
        self.open_contracts = max(0, self.open_contracts - qty)

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
            "peak_balance": self.peak_balance,
            "drawdown_floor": self.drawdown_floor,
            "distance_to_floor": equity - self.drawdown_floor,
            "daily_loss_limit": self.daily_loss_limit,
            "daily_loss_remaining": (
                (self.daily_loss_limit + self.day_pnl)
                if self.daily_loss_limit
                else None
            ),
            "open_contracts": self.open_contracts,
            "max_contracts": self.max_contracts,
            "trades_today": self.trades_today,
            "max_daily_trades": self.max_daily_trades,
            "locked": self.trading_locked,
            "lock_reason": self.lock_reason,
        }
