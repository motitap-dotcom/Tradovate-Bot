"""
Auto-Tuner — Continuous Learning System
=========================================
Analyzes trade journal data and automatically adjusts strategy parameters.

Runs at end-of-day (or on demand) and modifies config values for the next
trading session. All changes are logged and bounded by safety limits.

Features:
- Rolling window analysis (last N trading days, not all-time)
- Tunes ALL strategy params: stops, targets, cooldowns, trade caps, R:R, risk%
- Per-symbol per-hour performance gating (HourFilter)
- Intra-day streak detection (called by bot.py after each trade)
- Safety: capped at ±20% per cycle, hard min/max bounds, logged to tuner_log.json
"""

import json
import logging
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config
from trade_journal import TradeJournal

logger = logging.getLogger(__name__)

TUNER_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuner_log.json")

# ─────────────────────────────────────────
# Safety bounds for tunable parameters
# ─────────────────────────────────────────

# Per-symbol bounds (absolute min/max)
_BOUNDS = {
    "stop_loss_points": {
        "NQ": (15, 40), "ES": (4, 12), "GC": (3, 10), "CL": (0.10, 0.40),
    },
    "take_profit_points": {
        "NQ": (30, 80), "ES": (8, 24), "GC": (6, 20), "CL": (0.20, 0.80),
    },
    "orb_cooldown_minutes": {
        "NQ": (5, 60), "ES": (5, 60),
    },
    "vwap_cooldown_minutes": {
        "GC": (10, 120), "CL": (10, 120),
    },
    "max_orb_trades": {
        "NQ": (1, 4), "ES": (1, 4),
    },
    "max_vwap_trades_per_direction": {
        "GC": (1, 4), "CL": (1, 4),
    },
    "vwap_confirmation_candles": {
        "GC": (1, 3), "CL": (1, 3),
    },
    "risk_reward_ratio": {
        "NQ": (1.5, 4.0), "ES": (1.5, 4.0), "GC": (1.5, 4.0), "CL": (1.5, 4.0),
    },
}

# Global parameter bounds (not per-symbol)
_GLOBAL_BOUNDS = {
    "RISK_PER_TRADE_PCT": (0.005, 0.03),       # 0.5% to 3%
    "DAILY_LOSS_BRAKE_PCT": (0.40, 0.80),       # 40% to 80%
    "MAX_DAILY_TRADES": (4, 20),                 # 4 to 20
}

# Max adjustment per cycle: ±20%
MAX_ADJUST_PCT = 0.20

# Default rolling window (trading days)
DEFAULT_ROLLING_DAYS = 7


class AutoTuner:
    """Reads journal analytics and adjusts strategy parameters."""

    def __init__(self, journal: Optional[TradeJournal] = None):
        self.journal = journal or TradeJournal()
        self.adjustments: list[dict] = []

    def run(self, min_trades: int = 5, rolling_days: int = DEFAULT_ROLLING_DAYS) -> list[dict]:
        """
        Analyze recent performance and generate parameter adjustments.
        Uses rolling window (last N days) instead of all-time data.
        Returns list of adjustments made.
        """
        since = (date.today() - timedelta(days=rolling_days)).isoformat()
        closed = self.journal._closed_trades(since=since)

        # Only use trades with real P&L data (not legacy zeros)
        quality_trades = [t for t in closed if t.get("pnl") is not None and t["pnl"] != 0]

        if len(closed) < min_trades:
            logger.info("Auto-tuner: only %d trades in %d-day window (need %d). Skipping.",
                        len(closed), rolling_days, min_trades)
            return []

        data_quality = len(quality_trades) / len(closed) if closed else 0
        logger.info("Auto-tuner: %d trades (%d with P&L data, %.0f%% quality)",
                    len(closed), len(quality_trades), data_quality * 100)

        self.adjustments = []

        # Always run exit-reason-based tuning (works without P&L)
        self._tune_stops(closed)
        self._tune_daily_trade_cap(closed)

        # Only run P&L-dependent tuning if we have real data
        if data_quality >= 0.5:
            self._tune_targets(quality_trades)
            self._tune_symbol_allocation(quality_trades)
            self._tune_cooldowns(quality_trades)
            self._tune_risk_per_trade(quality_trades)
            self._tune_risk_reward(quality_trades)
            self._tune_trade_frequency(quality_trades)
        else:
            logger.info("Auto-tuner: data quality %.0f%% < 50%%, skipping P&L-based tuning",
                        data_quality * 100)

        if self.adjustments:
            self._apply_adjustments()
            self._log_adjustments()
            logger.info("Auto-tuner: %d adjustments applied", len(self.adjustments))
        else:
            logger.info("Auto-tuner: no adjustments needed")

        return self.adjustments

    # ─────────────────────────────────────────
    # Original tuning rules (improved)
    # ─────────────────────────────────────────

    def _tune_stops(self, closed: list[dict]):
        """Adjust stop-loss if too many trades hit SL or if stops are too wide."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            sl_trades = [t for t in trades if t.get("exit_reason") == "stop_loss"]
            tp_trades = [t for t in trades if t.get("exit_reason") == "take_profit"]
            total_exits = len(sl_trades) + len(tp_trades)

            if total_exits < 3:
                continue

            sl_rate = len(sl_trades) / total_exits

            current_sl = spec["stop_loss_points"]

            if sl_rate > 0.70:
                new_sl = current_sl * 1.10
                self._propose("stop_loss_points", sym, current_sl, new_sl,
                              f"SL hit rate {sl_rate:.0%} > 70%: widening stops")

            elif sl_rate < 0.30 and total_exits >= 5:
                new_sl = current_sl * 0.90
                self._propose("stop_loss_points", sym, current_sl, new_sl,
                              f"SL hit rate {sl_rate:.0%} < 30%: tightening stops")

    def _tune_targets(self, closed: list[dict]):
        """Adjust take-profit based on R-multiple and TP hit rate."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            all_with_r = [t for t in trades if t.get("r_multiple") is not None and t["r_multiple"] != 0]

            if len(all_with_r) < 3:
                continue

            avg_r = statistics.mean([t["r_multiple"] for t in all_with_r])
            current_tp = spec["take_profit_points"]

            if avg_r > 1.5:
                new_tp = current_tp * 1.10
                self._propose("take_profit_points", sym, current_tp, new_tp,
                              f"Avg R={avg_r:.2f} > 1.5: widening TP targets")
            elif avg_r < -0.5:
                new_tp = current_tp * 0.90
                self._propose("take_profit_points", sym, current_tp, new_tp,
                              f"Avg R={avg_r:.2f} < -0.5: tightening TP")

    def _tune_symbol_allocation(self, closed: list[dict]):
        """Flag consistently losing symbols for review."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            if len(trades) < 5:
                continue

            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            if not pnls:
                continue

            total_pnl = sum(pnls)
            win_rate = len([p for p in pnls if p > 0]) / len(pnls)

            if win_rate < 0.30 and total_pnl < -500:
                self.adjustments.append({
                    "param": "enabled",
                    "symbol": sym,
                    "old_value": True,
                    "new_value": "REVIEW",
                    "reason": f"Win rate {win_rate:.0%}, P&L ${total_pnl:+.0f} — consider disabling",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "applied": False,
                })

    def _tune_daily_trade_cap(self, closed: list[dict]):
        """Adjust max daily trades based on diminishing returns."""
        by_date = _group_by(closed, "date")

        late_trade_losses = 0
        late_trade_total = 0

        for dt, trades in by_date.items():
            if len(trades) >= 8:
                late = trades[7:]
                for t in late:
                    if t["pnl"] is not None:
                        late_trade_total += 1
                        if t["pnl"] < 0:
                            late_trade_losses += 1

        if late_trade_total >= 5 and late_trade_losses / late_trade_total > 0.70:
            current_cap = config.MAX_DAILY_TRADES
            new_cap = max(6, current_cap - 2)
            if new_cap != current_cap:
                self._propose_global("MAX_DAILY_TRADES", current_cap, new_cap,
                                     f"Late trades losing {late_trade_losses}/{late_trade_total}: reducing cap")

    # ─────────────────────────────────────────
    # NEW tuning rules
    # ─────────────────────────────────────────

    def _tune_cooldowns(self, closed: list[dict]):
        """Adjust cooldown periods based on back-to-back trade outcomes."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            # Sort by entry time
            sorted_trades = sorted(trades, key=lambda t: t.get("entry_time", ""))
            if len(sorted_trades) < 4:
                continue

            # Analyze consecutive trade pairs
            rapid_loss_pairs = 0
            rapid_total_pairs = 0

            for i in range(1, len(sorted_trades)):
                t1, t2 = sorted_trades[i - 1], sorted_trades[i]
                dur1 = t1.get("duration_minutes", 999)
                dur2 = t2.get("duration_minutes", 999)
                # "Rapid" = both trades closed within 30 minutes
                if dur1 < 30 and dur2 < 30:
                    rapid_total_pairs += 1
                    if (t1.get("pnl") or 0) < 0 and (t2.get("pnl") or 0) < 0:
                        rapid_loss_pairs += 1

            if rapid_total_pairs < 3:
                continue

            rapid_loss_rate = rapid_loss_pairs / rapid_total_pairs

            # Determine which cooldown param to adjust
            strategy = spec.get("strategy", "")
            if strategy == "ORB":
                param = "orb_cooldown_minutes"
            elif strategy == "VWAP":
                param = "vwap_cooldown_minutes"
            else:
                continue

            current = spec.get(param, 15)

            if rapid_loss_rate > 0.60:
                new_val = current * 1.15  # Widen cooldown 15%
                self._propose(param, sym, current, new_val,
                              f"Rapid consecutive losses {rapid_loss_rate:.0%}: widening cooldown")
            elif rapid_loss_rate < 0.20 and rapid_total_pairs >= 5:
                new_val = current * 0.90  # Tighten cooldown 10%
                self._propose(param, sym, current, new_val,
                              f"Rapid trades profitable ({1 - rapid_loss_rate:.0%} WR): tightening cooldown")

    def _tune_risk_per_trade(self, closed: list[dict]):
        """Adjust RISK_PER_TRADE_PCT based on overall performance."""
        pnls = [t["pnl"] for t in closed if t["pnl"] is not None]
        if len(pnls) < 10:
            return

        win_rate = len([p for p in pnls if p > 0]) / len(pnls)
        total_pnl = sum(pnls)
        current = config.RISK_PER_TRADE_PCT

        if win_rate > 0.55 and total_pnl > 0:
            # Performing well — cautious 5% increase
            new_val = current * 1.05
            self._propose_global("RISK_PER_TRADE_PCT", current, new_val,
                                 f"WR={win_rate:.0%}, P&L=${total_pnl:+.0f}: increasing risk")
        elif win_rate < 0.35 or total_pnl < -500:
            # Struggling — reduce risk 10%
            new_val = current * 0.90
            self._propose_global("RISK_PER_TRADE_PCT", current, new_val,
                                 f"WR={win_rate:.0%}, P&L=${total_pnl:+.0f}: reducing risk")

    def _tune_risk_reward(self, closed: list[dict]):
        """Adjust risk_reward_ratio per symbol based on actual R-multiples achieved."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            r_values = [t["r_multiple"] for t in trades
                        if t.get("r_multiple") is not None and t["r_multiple"] != 0]
            if len(r_values) < 5:
                continue

            avg_r = statistics.mean(r_values)
            current_rr = spec.get("risk_reward_ratio", 2.0)

            # If we consistently exceed the R:R target, widen it
            if avg_r > current_rr * 1.3:
                new_rr = current_rr * 1.10
                self._propose("risk_reward_ratio", sym, current_rr, new_rr,
                              f"Avg R={avg_r:.2f} >> target {current_rr}: widening R:R")
            # If we never reach it, tighten to lock in more wins
            elif avg_r < current_rr * 0.5 and avg_r > 0:
                new_rr = current_rr * 0.90
                self._propose("risk_reward_ratio", sym, current_rr, new_rr,
                              f"Avg R={avg_r:.2f} << target {current_rr}: tightening R:R")

    def _tune_trade_frequency(self, closed: list[dict]):
        """Adjust max trades per symbol based on diminishing returns per subsequent trade."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            # Group by date and analyze per-trade-number performance
            by_date = _group_by(trades, "date")
            first_trade_pnl = []
            later_trade_pnl = []

            for dt, day_trades in by_date.items():
                sorted_day = sorted(day_trades, key=lambda t: t.get("entry_time", ""))
                for i, t in enumerate(sorted_day):
                    pnl = t.get("pnl")
                    if pnl is None:
                        continue
                    if i == 0:
                        first_trade_pnl.append(pnl)
                    else:
                        later_trade_pnl.append(pnl)

            if len(first_trade_pnl) < 3 or len(later_trade_pnl) < 3:
                continue

            first_avg = statistics.mean(first_trade_pnl)
            later_avg = statistics.mean(later_trade_pnl)

            strategy = spec.get("strategy", "")
            if strategy == "ORB":
                param = "max_orb_trades"
            elif strategy == "VWAP":
                param = "max_vwap_trades_per_direction"
            else:
                continue

            current = spec.get(param, 2)

            # If later trades are significantly worse, reduce frequency
            if later_avg < 0 and first_avg > 0 and abs(later_avg) > first_avg * 0.5:
                new_val = max(1, current - 1)
                if new_val != current:
                    self._propose_int(param, sym, current, new_val,
                                      f"Later trades avg ${later_avg:+.0f} vs first ${first_avg:+.0f}: reducing")
            # If later trades are also profitable, allow more
            elif later_avg > 0 and first_avg > 0 and len(later_trade_pnl) >= 5:
                new_val = min(4, current + 1)
                if new_val != current:
                    self._propose_int(param, sym, current, new_val,
                                      f"Later trades profitable ${later_avg:+.0f}: allowing more")

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    def _propose(self, param: str, symbol: str, old_val: float, new_val: float, reason: str):
        """Propose a per-symbol adjustment, clamped to safety bounds."""
        # Cap change at ±MAX_ADJUST_PCT
        max_change = old_val * MAX_ADJUST_PCT
        new_val = max(old_val - max_change, min(old_val + max_change, new_val))

        # Apply absolute bounds
        bounds = _BOUNDS.get(param, {}).get(symbol)
        if bounds:
            new_val = max(bounds[0], min(bounds[1], new_val))

        # Round to appropriate precision
        spec = config.CONTRACT_SPECS.get(symbol, {})
        tick = spec.get("tick_size", 0.01)
        new_val = round(new_val / tick) * tick
        new_val = round(new_val, 4)

        if abs(new_val - old_val) < tick:
            return  # Change too small

        self.adjustments.append({
            "param": param,
            "symbol": symbol,
            "old_value": old_val,
            "new_value": new_val,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "applied": True,
        })

    def _propose_int(self, param: str, symbol: str, old_val: int, new_val: int, reason: str):
        """Propose an integer parameter adjustment (trade counts, candles, etc.)."""
        bounds = _BOUNDS.get(param, {}).get(symbol)
        if bounds:
            new_val = max(bounds[0], min(bounds[1], new_val))

        new_val = int(new_val)
        if new_val == old_val:
            return

        self.adjustments.append({
            "param": param,
            "symbol": symbol,
            "old_value": old_val,
            "new_value": new_val,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "applied": True,
        })

    def _propose_global(self, param: str, old_val: float, new_val: float, reason: str):
        """Propose a global config adjustment."""
        max_change = old_val * MAX_ADJUST_PCT
        new_val = max(old_val - max_change, min(old_val + max_change, new_val))

        bounds = _GLOBAL_BOUNDS.get(param)
        if bounds:
            new_val = max(bounds[0], min(bounds[1], new_val))

        # Round appropriately
        if isinstance(old_val, int) or param == "MAX_DAILY_TRADES":
            new_val = int(round(new_val))
            if new_val == old_val:
                return
        else:
            new_val = round(new_val, 4)
            if abs(new_val - old_val) < 0.0001:
                return

        self.adjustments.append({
            "param": param,
            "symbol": "global",
            "old_value": old_val,
            "new_value": new_val,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "applied": True,
        })

    def _apply_adjustments(self):
        """Apply parameter changes to config.CONTRACT_SPECS and global config."""
        for adj in self.adjustments:
            if not adj.get("applied"):
                continue
            sym = adj["symbol"]
            param = adj["param"]

            if sym == "global":
                # Global config params
                if hasattr(config, param):
                    setattr(config, param, adj["new_value"])
                    logger.info(
                        "Auto-tune global.%s: %s -> %s (%s)",
                        param, adj["old_value"], adj["new_value"], adj["reason"],
                    )
            elif sym in config.CONTRACT_SPECS and param in config.CONTRACT_SPECS[sym]:
                config.CONTRACT_SPECS[sym][param] = adj["new_value"]
                logger.info(
                    "Auto-tune %s.%s: %s -> %s (%s)",
                    sym, param, adj["old_value"], adj["new_value"], adj["reason"],
                )

    def _log_adjustments(self):
        """Append adjustments to persistent log file."""
        log = []
        if os.path.exists(TUNER_LOG):
            try:
                with open(TUNER_LOG) as f:
                    log = json.load(f)
            except (json.JSONDecodeError, TypeError):
                log = []

        log.extend(self.adjustments)

        # Keep last 500 entries (increased from 200 for richer history)
        log = log[-500:]

        with open(TUNER_LOG, "w") as f:
            json.dump(log, f, indent=2, default=str)


# ─────────────────────────────────────────
# Hour Filter — per-symbol per-hour gating
# ─────────────────────────────────────────

class HourFilter:
    """Tracks per-symbol per-hour performance and blocks bad hours."""

    def __init__(self, journal: TradeJournal):
        self.journal = journal
        self._blocked_hours: dict[str, set[int]] = {}  # symbol -> set of blocked hours

    def update(self, rolling_days: int = 10):
        """Analyze recent trades and update blocked hours."""
        since = (date.today() - timedelta(days=rolling_days)).isoformat()
        closed = self.journal._closed_trades(since=since)

        by_sym_hour: dict[tuple, list] = defaultdict(list)
        for t in closed:
            hour = t.get("entry_hour_et", -1)
            if hour < 0:
                continue
            by_sym_hour[(t["symbol"], hour)].append(t)

        self._blocked_hours = {}
        for (sym, hour), trades in by_sym_hour.items():
            pnls = [t["pnl"] for t in trades if t.get("pnl") is not None and t["pnl"] != 0]
            if len(pnls) < 3:
                continue  # Not enough data

            win_rate = len([p for p in pnls if p > 0]) / len(pnls)
            total_pnl = sum(pnls)

            # Block if win rate < 25% AND total P&L is negative
            if win_rate < 0.25 and total_pnl < 0:
                self._blocked_hours.setdefault(sym, set()).add(hour)
                logger.info(
                    "HourFilter: blocking %s at %d:00 ET (WR=%.0f%%, P&L=$%.0f, %d trades)",
                    sym, hour, win_rate * 100, total_pnl, len(pnls),
                )

    def is_allowed(self, symbol: str, hour_et: int) -> bool:
        """Check if trading this symbol at this hour is allowed."""
        blocked = self._blocked_hours.get(symbol, set())
        return hour_et not in blocked

    def get_blocked(self) -> dict[str, list[int]]:
        """Return blocked hours for logging/dashboard."""
        return {sym: sorted(hours) for sym, hours in self._blocked_hours.items()}


# ─────────────────────────────────────────
# Utility
# ─────────────────────────────────────────

def _group_by(items: list[dict], key: str) -> dict[str, list]:
    result: dict[str, list] = {}
    for item in items:
        k = item.get(key, "unknown")
        result.setdefault(k, []).append(item)
    return result
