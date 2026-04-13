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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config
from trade_journal import TradeJournal

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
TUNER_LOG = os.path.join(_HERE, "tuner_log.json")
TUNER_PENDING = os.path.join(_HERE, "tuner_pending.json")

# Safety bounds for each tunable parameter (absolute min/max)
_BOUNDS = {
    "stop_loss_points": {
        "NQ": (15, 40), "ES": (4, 12), "GC": (3, 10), "CL": (0.10, 0.40),
        "MNQ": (15, 40), "MES": (4, 12), "MGC": (3, 10), "MCL": (0.10, 0.40),
    },
    "take_profit_points": {
        "NQ": (30, 80), "ES": (8, 24), "GC": (6, 20), "CL": (0.20, 0.80),
        "MNQ": (30, 80), "MES": (8, 24), "MGC": (6, 20), "MCL": (0.20, 0.80),
    },
    "orb_cooldown_minutes": {
        "NQ": (5, 45), "ES": (5, 45),
        "MNQ": (5, 45), "MES": (5, 45),
    },
    "vwap_cooldown_minutes": {
        "GC": (10, 90), "CL": (10, 90),
        "MGC": (10, 90), "MCL": (10, 90),
    },
    "risk_reward_ratio": {
        "NQ": (1.5, 3.5), "ES": (1.5, 3.5), "GC": (1.5, 3.5), "CL": (1.5, 3.5),
        "MNQ": (1.5, 3.5), "MES": (1.5, 3.5), "MGC": (1.5, 3.5), "MCL": (1.5, 3.5),
    },
}

# Max adjustment per cycle: ±20%
MAX_ADJUST_PCT = 0.20


class AutoTuner:
    """Reads journal analytics and adjusts strategy parameters."""

    def __init__(self, journal: Optional[TradeJournal] = None):
        self.journal = journal or TradeJournal()
        self.adjustments: list[dict] = []
        self.applied_now: list[dict] = []
        self.rollbacks: list[dict] = []

    def run(self, min_trades: int = 20) -> list[dict]:
        """
        Analyze recent performance and generate parameter adjustments.

        Safety pipeline (added 2026-04-13 audit P2):
        1. Gate at ``min_trades`` (default 20) — raised from 5.
        2. Roll back any applied adjustment that degraded 3 rolling days.
        3. Stage proposals to ``tuner_pending.json``.
        4. Only apply a staged proposal when the *same* direction for the
           same (symbol, param) has been proposed in two consecutive daily
           cycles.

        Returns the list of adjustments that were actually applied in this
        cycle (may be empty even when new proposals were staged).
        """
        closed = self.journal._closed_trades()
        if len(closed) < min_trades:
            logger.info("Auto-tuner: only %d trades (need %d). Skipping.", len(closed), min_trades)
            return []

        # Step 1: rollback degraded applies before generating new proposals.
        self.rollbacks = self._rollback_if_degraded(closed)

        # Step 2: generate fresh proposals into self.adjustments.
        self.adjustments = []

        self._tune_stops(closed)
        self._tune_targets(closed)
        self._tune_stops_from_mae(closed)
        self._tune_targets_from_mfe(closed)
        self._tune_cooldowns(closed)
        self._tune_rr_ratio(closed)
        self._tune_time_window(closed)
        self._tune_symbol_allocation(closed)
        self._tune_daily_trade_cap(closed)

        # Step 3: reconcile proposals with the pending store; apply any
        # proposal confirmed by two consecutive cycles.
        self.applied_now = self._reconcile_pending(self.adjustments)

        if self.applied_now:
            self._apply_adjustments(self.applied_now)
            self._log_adjustments(self.applied_now)
            logger.info(
                "Auto-tuner: %d adjustments applied (confirmed by shadow cycle)",
                len(self.applied_now),
            )
        else:
            logger.info(
                "Auto-tuner: %d proposals staged, 0 applied this cycle",
                len(self.adjustments),
            )

        return self.applied_now

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

    def _tune_stops_from_mae(self, closed: list[dict]):
        """Use MAE (Maximum Adverse Excursion) data to optimize stop distances.

        If winning trades show MAE deeper than current stop, stops are too tight.
        If winning trades show MAE much shallower than stop, stops can be tightened.
        """
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            # Only use trades with MAE data
            winners_with_mae = [
                t for t in trades
                if t.get("pnl") and t["pnl"] > 0 and t.get("mae_points") is not None
            ]
            if len(winners_with_mae) < 5:
                continue

            # MAE is negative (how far against us). Get absolute values.
            mae_values = [abs(t["mae_points"]) for t in winners_with_mae]
            current_sl = spec["stop_loss_points"]

            # The 90th percentile of winning trade MAE = optimal stop distance
            # (covers 90% of winners while cutting the rest)
            mae_values.sort()
            idx_90 = int(len(mae_values) * 0.90)
            optimal_stop = mae_values[idx_90] * 1.05  # 5% buffer

            if optimal_stop > current_sl * 1.15:
                # Stops are too tight — winners are seeing deeper drawdowns
                self._propose("stop_loss_points", sym, current_sl, optimal_stop,
                              f"MAE analysis: 90th pct={optimal_stop:.2f} > SL={current_sl:.2f} — widening")

            elif optimal_stop < current_sl * 0.80 and len(winners_with_mae) >= 10:
                # Stops can be tightened — winners don't draw down that far
                self._propose("stop_loss_points", sym, current_sl, optimal_stop,
                              f"MAE analysis: 90th pct={optimal_stop:.2f} < SL={current_sl:.2f} — tightening")

    def _tune_targets_from_mfe(self, closed: list[dict]):
        """Use MFE (Maximum Favorable Excursion) to optimize take-profit distances.

        If trades see MFE well beyond TP, we're leaving money on the table.
        If trades rarely reach MFE near TP, targets are too ambitious.
        """
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            trades_with_mfe = [
                t for t in trades
                if t.get("mfe_points") is not None and t["mfe_points"] > 0
            ]
            if len(trades_with_mfe) < 5:
                continue

            mfe_values = [t["mfe_points"] for t in trades_with_mfe]
            current_tp = spec["take_profit_points"]
            avg_mfe = statistics.mean(mfe_values)
            median_mfe = statistics.median(mfe_values)

            # If median MFE exceeds TP by 20%+, we can widen targets
            if median_mfe > current_tp * 1.20:
                new_tp = median_mfe * 0.90  # Target at 90% of median MFE
                self._propose("take_profit_points", sym, current_tp, new_tp,
                              f"MFE analysis: median MFE={median_mfe:.2f} >> TP={current_tp:.2f} — widening")

            # If median MFE is well below TP, targets are too ambitious
            elif median_mfe < current_tp * 0.60 and len(trades_with_mfe) >= 10:
                new_tp = median_mfe * 0.85  # More conservative
                self._propose("take_profit_points", sym, current_tp, new_tp,
                              f"MFE analysis: median MFE={median_mfe:.2f} << TP={current_tp:.2f} — tightening")

    def _tune_cooldowns(self, closed: list[dict]):
        """Optimize cooldown periods between trades.

        If second trades in a session consistently lose, increase cooldown.
        If second trades consistently win, decrease cooldown.
        """
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            strategy = spec.get("strategy")
            cooldown_key = "orb_cooldown_minutes" if strategy == "ORB" else "vwap_cooldown_minutes"
            current_cd = spec.get(cooldown_key)
            if current_cd is None:
                continue

            # Group trades by date and check performance of 2nd+ trades
            by_date = _group_by(trades, "date")
            second_trades = []
            for dt, day_trades in by_date.items():
                if len(day_trades) >= 2:
                    second_trades.extend(day_trades[1:])

            if len(second_trades) < 5:
                continue

            second_pnls = [t["pnl"] for t in second_trades if t["pnl"] is not None]
            if not second_pnls:
                continue

            second_win_rate = len([p for p in second_pnls if p > 0]) / len(second_pnls)

            if second_win_rate < 0.30:
                # Second trades mostly lose → increase cooldown
                new_cd = current_cd * 1.15
                self._propose(cooldown_key, sym, current_cd, new_cd,
                              f"2nd+ trade WR={second_win_rate:.0%} < 30%: increasing cooldown")

            elif second_win_rate > 0.55 and len(second_pnls) >= 8:
                # Second trades profitable → decrease cooldown
                new_cd = current_cd * 0.85
                self._propose(cooldown_key, sym, current_cd, new_cd,
                              f"2nd+ trade WR={second_win_rate:.0%} > 55%: decreasing cooldown")

    def _tune_rr_ratio(self, closed: list[dict]):
        """Optimize risk/reward ratio based on actual R-multiples achieved."""
        by_sym = _group_by(closed, "symbol")

        for sym, trades in by_sym.items():
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec or not spec.get("enabled"):
                continue

            with_r = [t for t in trades if t.get("r_multiple") is not None and t["r_multiple"] != 0]
            if len(with_r) < 8:
                continue

            current_rr = spec.get("risk_reward_ratio", 2.0)
            avg_r = statistics.mean([t["r_multiple"] for t in with_r])
            win_r = [t["r_multiple"] for t in with_r if t["r_multiple"] > 0]

            if not win_r:
                continue

            avg_win_r = statistics.mean(win_r)

            # If average winning R is much higher than RR ratio, we can target more
            if avg_win_r > current_rr * 1.3:
                new_rr = min(avg_win_r * 0.85, current_rr * 1.15)
                self._propose("risk_reward_ratio", sym, current_rr, new_rr,
                              f"Avg winning R={avg_win_r:.2f} > RR={current_rr:.1f}: increasing RR")

            # If average R is very negative, maybe RR is too ambitious
            elif avg_r < -0.5 and avg_win_r < current_rr * 0.6:
                new_rr = max(avg_win_r * 1.1, current_rr * 0.85)
                self._propose("risk_reward_ratio", sym, current_rr, new_rr,
                              f"Avg R={avg_r:.2f}, avg win R={avg_win_r:.2f}: reducing RR")

    def _tune_time_window(self, closed: list[dict]):
        """Identify best/worst trading hours and log recommendations.

        Doesn't auto-adjust (too risky), but flags hours to avoid.
        """
        by_hour = {}
        for t in closed:
            hour = t.get("entry_hour_et")
            if hour is None:
                continue
            by_hour.setdefault(hour, []).append(t)

        for hour, trades in by_hour.items():
            if len(trades) < 5:
                continue

            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            if not pnls:
                continue

            total_pnl = sum(pnls)
            win_rate = len([p for p in pnls if p > 0]) / len(pnls)

            if total_pnl < -200 and win_rate < 0.30:
                self.adjustments.append({
                    "param": "trading_hours",
                    "symbol": "global",
                    "old_value": f"hour={hour}",
                    "new_value": "AVOID",
                    "reason": (
                        f"Hour {hour}:00 ET: {len(trades)} trades, "
                        f"WR={win_rate:.0%}, P&L=${total_pnl:+.0f} — consider avoiding"
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "applied": False,  # Advisory only
                })

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
        """Propose an adjustment, clamped to safety bounds.

        Note: this only *stages* the proposal (append to
        ``self.adjustments``). Actual mutation of ``config.CONTRACT_SPECS``
        happens in ``_apply_adjustments`` once the proposal has been
        confirmed by a second cycle (see ``_reconcile_pending``).
        ``applied=False`` is the staged state; ``_reconcile_pending``
        flips it to True when the proposal clears the shadow cycle.
        """
        # Cap change at ±MAX_ADJUST_PCT
        max_change = old_val * MAX_ADJUST_PCT
        new_val = max(old_val - max_change, min(old_val + max_change, new_val))

        # Apply absolute bounds
        bounds = _BOUNDS.get(param, {}).get(symbol)
        if bounds:
            new_val = max(bounds[0], min(bounds[1], new_val))

        # Round to appropriate precision
        spec = config.CONTRACT_SPECS.get(symbol, {})
        if param in ("orb_cooldown_minutes", "vwap_cooldown_minutes"):
            new_val = int(round(new_val))
            min_change = 1
        elif param == "risk_reward_ratio":
            new_val = round(new_val, 2)
            min_change = 0.05
        else:
            tick = spec.get("tick_size", 0.01)
            new_val = round(new_val / tick) * tick
            new_val = round(new_val, 4)
            min_change = tick

        if abs(new_val - old_val) < min_change:
            return  # Change too small

        self.adjustments.append({
            "param": param,
            "symbol": symbol,
            "old_value": old_val,
            "new_value": new_val,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "applied": False,  # staged — _reconcile_pending promotes later
        })

    # ─────────────────────────────────────────
    # Shadow-test pipeline
    # ─────────────────────────────────────────

    @staticmethod
    def _key(adj: dict) -> str:
        return f"{adj['symbol']}::{adj['param']}"

    @staticmethod
    def _direction(old_val, new_val) -> str:
        if new_val > old_val:
            return "up"
        if new_val < old_val:
            return "down"
        return "flat"

    def _load_pending(self) -> dict:
        if not os.path.exists(TUNER_PENDING):
            return {}
        try:
            with open(TUNER_PENDING) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_pending(self, pending: dict):
        try:
            tmp = TUNER_PENDING + ".tmp"
            with open(tmp, "w") as f:
                json.dump(pending, f, indent=2, default=str)
            os.replace(tmp, TUNER_PENDING)
        except OSError as e:
            logger.warning("Failed to persist tuner_pending.json: %s", e)

    def _reconcile_pending(self, staged: list[dict]) -> list[dict]:
        """Compare staged proposals against the pending store.

        - First time a direction is seen for a key → record it as pending.
        - Second time the *same* direction is seen → promote it to applied
          and clear the pending entry.
        - A proposal in the opposite direction wipes the pending entry.
        - Keys not touched this cycle age out after 7 days.
        """
        pending = self._load_pending()
        now = datetime.now(timezone.utc)
        applied: list[dict] = []

        # Age out stale entries (> 7d without confirmation)
        cutoff = now - timedelta(days=7)
        stale_keys = []
        for key, entry in pending.items():
            try:
                ts = datetime.fromisoformat(entry["first_seen"])
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                stale_keys.append(key)
        for key in stale_keys:
            pending.pop(key, None)

        for adj in staged:
            # Advisory proposals (trading_hours, enabled=REVIEW) and
            # already-applied legacy proposals (MAX_DAILY_TRADES) bypass
            # the shadow cycle.
            if adj.get("applied") is True:
                applied.append(adj)
                continue
            new_val = adj.get("new_value")
            if not isinstance(new_val, (int, float)):
                # Advisory / non-numeric — leave as-is, don't reconcile.
                continue

            key = self._key(adj)
            direction = self._direction(adj["old_value"], adj["new_value"])
            entry = pending.get(key)

            if entry is None:
                pending[key] = {
                    "direction": direction,
                    "first_seen": now.isoformat(),
                    "proposed_value": adj["new_value"],
                    "from_value": adj["old_value"],
                    "reason": adj["reason"],
                }
                adj["pending"] = True
                continue

            if entry["direction"] != direction:
                # Direction flipped → drop the pending, start over next cycle.
                pending.pop(key, None)
                adj["pending"] = True
                pending[key] = {
                    "direction": direction,
                    "first_seen": now.isoformat(),
                    "proposed_value": adj["new_value"],
                    "from_value": adj["old_value"],
                    "reason": adj["reason"],
                }
                continue

            # Direction matches — promote to applied.
            adj["applied"] = True
            adj["confirmed_after_cycles"] = 2
            applied.append(adj)
            pending.pop(key, None)

        self._save_pending(pending)
        return applied

    def _apply_adjustments(self, adjustments: list[dict]):
        """Apply confirmed parameter changes to config.CONTRACT_SPECS."""
        for adj in adjustments:
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

    def _log_adjustments(self, adjustments: list[dict]):
        """Append adjustments (applied + rollbacks) to persistent log."""
        log = []
        if os.path.exists(TUNER_LOG):
            try:
                with open(TUNER_LOG) as f:
                    log = json.load(f)
            except (json.JSONDecodeError, TypeError):
                log = []

        log.extend(adjustments)
        log.extend(self.rollbacks)

        # Keep last 200 entries
        log = log[-200:]

        try:
            tmp = TUNER_LOG + ".tmp"
            with open(tmp, "w") as f:
                json.dump(log, f, indent=2, default=str)
            os.replace(tmp, TUNER_LOG)
        except OSError as e:
            logger.warning("Failed to persist tuner_log.json: %s", e)

    # ─────────────────────────────────────────
    # Rollback — revert applies that degraded performance
    # ─────────────────────────────────────────

    def _rollback_if_degraded(self, closed: list[dict]) -> list[dict]:
        """Scan the tuner_log for the most recent applied change per key.

        If the symbol has had 3 consecutive losing days *since* that apply,
        revert the parameter and log a rollback record.
        Returns the list of rollback records that were applied.
        """
        if not os.path.exists(TUNER_LOG):
            return []
        try:
            with open(TUNER_LOG) as f:
                log = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

        # Most recent applied change per (symbol, param)
        latest: dict[str, dict] = {}
        for entry in log:
            if not entry.get("applied"):
                continue
            if entry.get("param") in {"trading_hours", "enabled"}:
                # advisory-only — not directly revertible
                continue
            key = f"{entry.get('symbol')}::{entry.get('param')}"
            latest[key] = entry

        rollbacks: list[dict] = []
        for key, entry in latest.items():
            sym = entry["symbol"]
            param = entry["param"]
            try:
                applied_at = datetime.fromisoformat(entry["timestamp"])
            except (KeyError, ValueError):
                continue

            # Daily P&L for this symbol since the apply date (ET day)
            applied_date = applied_at.date().isoformat()
            sym_trades = [
                t for t in closed
                if t.get("symbol") == sym and t.get("date", "") >= applied_date
            ]
            if not sym_trades:
                continue

            by_date: dict[str, float] = {}
            for t in sym_trades:
                d = t.get("date")
                pnl = t.get("pnl") or 0
                by_date[d] = by_date.get(d, 0) + pnl

            # Check the last 3 days (chronologically) for the symbol
            recent_days = sorted(by_date.items())[-3:]
            if len(recent_days) < 3:
                continue
            if not all(pnl < 0 for _, pnl in recent_days):
                continue

            # Revert! — write the old value back to config
            if sym in config.CONTRACT_SPECS and param in config.CONTRACT_SPECS[sym]:
                old_live = config.CONTRACT_SPECS[sym][param]
                config.CONTRACT_SPECS[sym][param] = entry["old_value"]
                logger.warning(
                    "Auto-tune ROLLBACK %s.%s: %s -> %s (3 losing days since %s)",
                    sym, param, old_live, entry["old_value"], applied_date,
                )
                rollbacks.append({
                    "param": param,
                    "symbol": sym,
                    "old_value": old_live,
                    "new_value": entry["old_value"],
                    "reason": (
                        f"ROLLBACK: 3 losing days since apply on {applied_date} "
                        f"({[(d, round(p, 2)) for d, p in recent_days]})"
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "applied": True,
                    "rollback_of": entry.get("timestamp"),
                })

        return rollbacks


def _group_by(items: list[dict], key: str) -> dict[str, list]:
    result = {}
    for item in items:
        k = item.get(key, "unknown")
        result.setdefault(k, []).append(item)
    return result
