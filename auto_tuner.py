"""
Auto-Tuner
===========
Analyzes trade journal data and automatically adjusts strategy parameters.

Runs at end-of-day (or on demand) and modifies config values for the next
trading session. All changes are logged and bounded by safety limits.

Safety:
- Adjustments are capped at ±20% per cycle
- Hard min/max bounds prevent dangerous values
- Changes are logged to tuner_log.json for review
"""

import json
import logging
import os
import statistics
from datetime import date, datetime, timezone
from typing import Optional

import config
from trade_journal import TradeJournal

logger = logging.getLogger(__name__)

TUNER_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuner_log.json")

# Safety bounds for each tunable parameter (absolute min/max)
_BOUNDS = {
    "stop_loss_points": {
        "NQ": (15, 40), "ES": (4, 12), "GC": (3, 10), "CL": (0.10, 0.40),
    },
    "take_profit_points": {
        "NQ": (30, 80), "ES": (8, 24), "GC": (6, 20), "CL": (0.20, 0.80),
    },
}

# Max adjustment per cycle: ±20%
MAX_ADJUST_PCT = 0.20


class AutoTuner:
    """Reads journal analytics and adjusts strategy parameters."""

    def __init__(self, journal: Optional[TradeJournal] = None):
        self.journal = journal or TradeJournal()
        self.adjustments: list[dict] = []

    def run(self, min_trades: int = 5) -> list[dict]:
        """
        Analyze recent performance and generate parameter adjustments.
        Returns list of adjustments made.
        """
        closed = self.journal._closed_trades()
        if len(closed) < min_trades:
            logger.info("Auto-tuner: only %d trades (need %d). Skipping.", len(closed), min_trades)
            return []

        self.adjustments = []

        self._tune_stops(closed)
        self._tune_targets(closed)
        self._tune_symbol_allocation(closed)
        self._tune_daily_trade_cap(closed)

        if self.adjustments:
            self._apply_adjustments()
            self._log_adjustments()
            logger.info("Auto-tuner: %d adjustments applied", len(self.adjustments))
        else:
            logger.info("Auto-tuner: no adjustments needed")

        return self.adjustments

    # ─────────────────────────────────────────
    # Tuning rules
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
                # Too many stop-outs → widen stops by 10%
                new_sl = current_sl * 1.10
                self._propose("stop_loss_points", sym, current_sl, new_sl,
                              f"SL hit rate {sl_rate:.0%} > 70%: widening stops")

            elif sl_rate < 0.30 and total_exits >= 5:
                # Stops rarely hit → could tighten for better risk/reward
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

            tp_trades = [t for t in trades if t.get("exit_reason") == "take_profit"]
            all_with_r = [t for t in trades if t.get("r_multiple") is not None and t["r_multiple"] != 0]

            if len(all_with_r) < 3:
                continue

            avg_r = statistics.mean([t["r_multiple"] for t in all_with_r])
            current_tp = spec["take_profit_points"]

            if avg_r > 1.5:
                # Consistently exceeding TP → could widen for more profit
                new_tp = current_tp * 1.10
                self._propose("take_profit_points", sym, current_tp, new_tp,
                              f"Avg R={avg_r:.2f} > 1.5: widening TP targets")

            elif avg_r < 0.5 and len(tp_trades) > 0:
                # Low R but hitting TP → TP might be too tight
                pass  # Don't tighten TP if we're hitting it

            elif avg_r < -0.5:
                # Negative R → tighten TP to lock in more wins
                new_tp = current_tp * 0.90
                self._propose("take_profit_points", sym, current_tp, new_tp,
                              f"Avg R={avg_r:.2f} < -0.5: tightening TP")

    def _tune_symbol_allocation(self, closed: list[dict]):
        """Disable consistently losing symbols."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            if len(trades) < 5:
                continue

            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            if not pnls:
                continue

            total_pnl = sum(pnls)
            win_rate = len([p for p in pnls if p > 0]) / len(pnls)

            # If both win rate AND total P&L are bad → flag for review
            if win_rate < 0.30 and total_pnl < -500:
                self.adjustments.append({
                    "param": "enabled",
                    "symbol": sym,
                    "old_value": True,
                    "new_value": "REVIEW",  # Don't auto-disable, just flag
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
                # Check if late trades (after 8th) are profitable
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
                self.adjustments.append({
                    "param": "MAX_DAILY_TRADES",
                    "symbol": "global",
                    "old_value": current_cap,
                    "new_value": new_cap,
                    "reason": f"Late trades losing {late_trade_losses}/{late_trade_total}: reducing cap",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "applied": True,
                })
                config.MAX_DAILY_TRADES = new_cap

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    def _propose(self, param: str, symbol: str, old_val: float, new_val: float, reason: str):
        """Propose an adjustment, clamped to safety bounds."""
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

    def _apply_adjustments(self):
        """Apply parameter changes to config.CONTRACT_SPECS."""
        for adj in self.adjustments:
            if not adj.get("applied"):
                continue
            sym = adj["symbol"]
            param = adj["param"]
            if sym in config.CONTRACT_SPECS and param in config.CONTRACT_SPECS[sym]:
                config.CONTRACT_SPECS[sym][param] = adj["new_value"]
                logger.info(
                    "Auto-tune %s.%s: %.4f -> %.4f (%s)",
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

        # Keep last 200 entries
        log = log[-200:]

        with open(TUNER_LOG, "w") as f:
            json.dump(log, f, indent=2, default=str)


def _group_by(items: list[dict], key: str) -> dict[str, list]:
    result = {}
    for item in items:
        k = item.get(key, "unknown")
        result.setdefault(k, []).append(item)
    return result
