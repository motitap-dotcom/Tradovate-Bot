"""
Continuous Learning Engine
===========================
Structured system for learning from every trade and continuously improving
bot parameters. Runs automatically at end-of-day and generates actionable
insights per parameter, per symbol, and per strategy.

Architecture:
  TradeJournal (data) -> ContinuousLearner (analysis) -> AutoTuner (action)
                                |
                         learning_report.json (persistent insights)

Usage:
    # In bot.py — runs automatically at end-of-day:
    from continuous_learner import ContinuousLearner
    learner = ContinuousLearner(journal)
    report = learner.run_daily_analysis()

    # Weekly deep analysis:
    report = learner.run_weekly_analysis()
"""

import json
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import config
from trade_journal import TradeJournal
from auto_tuner import AutoTuner

logger = logging.getLogger(__name__)

REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learning_report.json")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learning_history.json")


class ContinuousLearner:
    """Analyzes trades and generates structured learning insights."""

    def __init__(self, journal: Optional[TradeJournal] = None):
        self.journal = journal or TradeJournal()
        self.insights: list[dict] = []
        self.parameter_scores: dict[str, dict] = {}

    def run_daily_analysis(self) -> dict:
        """End-of-day analysis: quick insights + auto-tuner run."""
        today = date.today().isoformat()
        closed = self.journal._closed_trades(since=today)
        all_closed = self.journal._closed_trades()

        report = {
            "type": "daily",
            "date": today,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trades_today": len(closed),
            "insights": [],
            "parameter_analysis": {},
            "tuner_adjustments": [],
            "streak_info": {},
            "score": {},
        }

        if not closed:
            report["insights"].append({
                "level": "info",
                "message": "No trades today.",
            })
            self._save_report(report)
            return report

        # Daily P&L
        pnls = [t["pnl"] for t in closed if t["pnl"] is not None]
        report["daily_pnl"] = sum(pnls) if pnls else 0

        # Analyze each parameter dimension
        report["parameter_analysis"] = self._analyze_all_parameters(all_closed)

        # Streak analysis
        report["streak_info"] = self.journal.analyze_streaks()

        # Generate insights
        report["insights"] = self._generate_daily_insights(closed, all_closed)

        # Run auto-tuner
        tuner = AutoTuner(self.journal)
        adjustments = tuner.run()
        report["tuner_adjustments"] = adjustments

        # Score today's performance
        report["score"] = self._score_day(closed, all_closed)

        self._save_report(report)
        self._append_history(report)

        logger.info(
            "Daily learning: %d trades, P&L=$%.2f, %d insights, %d adjustments",
            len(closed), report.get("daily_pnl", 0),
            len(report["insights"]), len(adjustments),
        )
        return report

    def run_weekly_analysis(self) -> dict:
        """Weekly deep analysis: trends, parameter effectiveness, recommendations."""
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        recent = self.journal._closed_trades(since=week_ago)
        all_closed = self.journal._closed_trades()

        report = {
            "type": "weekly",
            "date": date.today().isoformat(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trades_this_week": len(recent),
            "insights": [],
            "parameter_analysis": {},
            "trend_analysis": {},
            "recommendations": [],
        }

        if len(recent) < 3:
            report["insights"].append({
                "level": "info",
                "message": f"Only {len(recent)} trades this week. Need more data.",
            })
            self._save_report(report)
            return report

        # Deep parameter analysis
        report["parameter_analysis"] = self._analyze_all_parameters(all_closed)

        # Trend analysis: is performance improving or degrading?
        report["trend_analysis"] = self._analyze_trends(all_closed)

        # Strategic recommendations
        report["recommendations"] = self._generate_recommendations(recent, all_closed)

        # Week insights
        report["insights"] = self._generate_weekly_insights(recent, all_closed)

        self._save_report(report)
        self._append_history(report)

        logger.info(
            "Weekly learning: %d trades, %d insights, %d recommendations",
            len(recent), len(report["insights"]), len(report["recommendations"]),
        )
        return report

    # ─────────────────────────────────────────
    # Parameter Analysis
    # ─────────────────────────────────────────

    def _analyze_all_parameters(self, closed: list[dict]) -> dict:
        """Analyze effectiveness of every tunable parameter."""
        result = {}

        for sym in config.CONTRACT_SPECS:
            spec = config.CONTRACT_SPECS[sym]
            if not spec.get("enabled"):
                continue

            sym_trades = [t for t in closed if t["symbol"] == sym]
            if len(sym_trades) < 3:
                continue

            result[sym] = {
                "stop_loss": self._analyze_stop_loss(sym, sym_trades, spec),
                "take_profit": self._analyze_take_profit(sym, sym_trades, spec),
                "cooldown": self._analyze_cooldown(sym, sym_trades, spec),
                "risk_reward": self._analyze_rr(sym, sym_trades, spec),
                "time_of_day": self._analyze_time_performance(sym_trades),
                "day_of_week": self._analyze_dow_performance(sym_trades),
            }

        return result

    def _analyze_stop_loss(self, sym: str, trades: list[dict], spec: dict) -> dict:
        """Analyze stop-loss effectiveness for a symbol."""
        current_sl = spec["stop_loss_points"]
        sl_hits = [t for t in trades if t.get("exit_reason") == "stop_loss"]
        tp_hits = [t for t in trades if t.get("exit_reason") == "take_profit"]
        total_exits = len(sl_hits) + len(tp_hits)

        # MAE analysis
        winners_mae = [abs(t["mae_points"]) for t in trades
                       if t.get("pnl") and t["pnl"] > 0 and t.get("mae_points") is not None]

        result = {
            "current_value": current_sl,
            "sl_hit_rate": len(sl_hits) / total_exits if total_exits else 0,
            "total_sl_hits": len(sl_hits),
            "verdict": "ok",
        }

        if winners_mae:
            result["winner_mae_90pct"] = round(sorted(winners_mae)[int(len(winners_mae) * 0.9)], 4)
            result["winner_mae_avg"] = round(statistics.mean(winners_mae), 4)

        if total_exits >= 5:
            sl_rate = result["sl_hit_rate"]
            if sl_rate > 0.70:
                result["verdict"] = "too_tight"
                result["recommendation"] = f"SL hit rate {sl_rate:.0%} is too high. Consider widening."
            elif sl_rate < 0.25:
                result["verdict"] = "could_tighten"
                result["recommendation"] = f"SL hit rate {sl_rate:.0%} is very low. Could tighten for better RR."

        return result

    def _analyze_take_profit(self, sym: str, trades: list[dict], spec: dict) -> dict:
        """Analyze take-profit effectiveness."""
        current_tp = spec["take_profit_points"]
        tp_hits = [t for t in trades if t.get("exit_reason") == "take_profit"]

        # MFE analysis
        all_mfe = [t["mfe_points"] for t in trades
                   if t.get("mfe_points") is not None and t["mfe_points"] > 0]

        result = {
            "current_value": current_tp,
            "tp_hit_count": len(tp_hits),
            "verdict": "ok",
        }

        if all_mfe:
            result["avg_mfe"] = round(statistics.mean(all_mfe), 4)
            result["median_mfe"] = round(statistics.median(all_mfe), 4)
            median = result["median_mfe"]

            if median > current_tp * 1.3:
                result["verdict"] = "too_tight"
                result["recommendation"] = (
                    f"Median MFE ({median:.2f}) far exceeds TP ({current_tp:.2f}). "
                    "Leaving profit on table."
                )
            elif median < current_tp * 0.5:
                result["verdict"] = "too_wide"
                result["recommendation"] = (
                    f"Median MFE ({median:.2f}) well below TP ({current_tp:.2f}). "
                    "Targets may be unrealistic."
                )

        return result

    def _analyze_cooldown(self, sym: str, trades: list[dict], spec: dict) -> dict:
        """Analyze cooldown effectiveness."""
        strategy = spec.get("strategy")
        cd_key = "orb_cooldown_minutes" if strategy == "ORB" else "vwap_cooldown_minutes"
        current_cd = spec.get(cd_key, 0)

        by_date = defaultdict(list)
        for t in trades:
            by_date[t.get("date", "")].append(t)

        first_trade_pnls = []
        later_trade_pnls = []
        for dt, day_trades in by_date.items():
            if day_trades:
                first_pnl = day_trades[0].get("pnl")
                if first_pnl is not None:
                    first_trade_pnls.append(first_pnl)
            for t in day_trades[1:]:
                if t.get("pnl") is not None:
                    later_trade_pnls.append(t["pnl"])

        result = {
            "current_value": current_cd,
            "first_trade_avg_pnl": round(statistics.mean(first_trade_pnls), 2) if first_trade_pnls else 0,
            "later_trade_avg_pnl": round(statistics.mean(later_trade_pnls), 2) if later_trade_pnls else 0,
            "verdict": "ok",
        }

        if later_trade_pnls and first_trade_pnls:
            if result["later_trade_avg_pnl"] < -50 and result["first_trade_avg_pnl"] > 0:
                result["verdict"] = "increase_cooldown"
                result["recommendation"] = "Later trades losing. Consider longer cooldown."
            elif result["later_trade_avg_pnl"] > result["first_trade_avg_pnl"] * 0.8:
                result["verdict"] = "could_decrease"
                result["recommendation"] = "Later trades performing well. Could reduce cooldown."

        return result

    def _analyze_rr(self, sym: str, trades: list[dict], spec: dict) -> dict:
        """Analyze risk/reward ratio effectiveness."""
        current_rr = spec.get("risk_reward_ratio", 2.0)
        with_r = [t for t in trades if t.get("r_multiple") is not None and t["r_multiple"] != 0]

        result = {
            "current_value": current_rr,
            "avg_r_multiple": 0,
            "verdict": "ok",
        }

        if with_r:
            r_values = [t["r_multiple"] for t in with_r]
            result["avg_r_multiple"] = round(statistics.mean(r_values), 2)
            result["median_r_multiple"] = round(statistics.median(r_values), 2)
            win_r = [r for r in r_values if r > 0]
            if win_r:
                result["avg_winning_r"] = round(statistics.mean(win_r), 2)

        return result

    def _analyze_time_performance(self, trades: list[dict]) -> dict:
        """Find best/worst trading hours."""
        by_hour = defaultdict(list)
        for t in trades:
            h = t.get("entry_hour_et")
            if h is not None:
                by_hour[h].append(t.get("pnl", 0) or 0)

        result = {}
        for h, pnls in sorted(by_hour.items()):
            if len(pnls) >= 3:
                result[str(h)] = {
                    "trades": len(pnls),
                    "total_pnl": round(sum(pnls), 2),
                    "win_rate": round(len([p for p in pnls if p > 0]) / len(pnls), 2),
                }
        return result

    def _analyze_dow_performance(self, trades: list[dict]) -> dict:
        """Find best/worst trading days of week."""
        by_dow = defaultdict(list)
        for t in trades:
            dow = t.get("entry_day_of_week")
            if dow:
                by_dow[dow].append(t.get("pnl", 0) or 0)

        result = {}
        for dow, pnls in by_dow.items():
            if len(pnls) >= 3:
                result[dow] = {
                    "trades": len(pnls),
                    "total_pnl": round(sum(pnls), 2),
                    "win_rate": round(len([p for p in pnls if p > 0]) / len(pnls), 2),
                }
        return result

    # ─────────────────────────────────────────
    # Trend Analysis
    # ─────────────────────────────────────────

    def _analyze_trends(self, closed: list[dict]) -> dict:
        """Compare recent performance to overall to detect improving/degrading trends."""
        if len(closed) < 10:
            return {"status": "insufficient_data"}

        # Split into first half and second half
        mid = len(closed) // 2
        first_half = closed[:mid]
        second_half = closed[mid:]

        def _stats(trades):
            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            wins = [p for p in pnls if p > 0]
            return {
                "trades": len(trades),
                "win_rate": len(wins) / len(pnls) if pnls else 0,
                "avg_pnl": statistics.mean(pnls) if pnls else 0,
                "total_pnl": sum(pnls) if pnls else 0,
            }

        first = _stats(first_half)
        second = _stats(second_half)

        wr_change = second["win_rate"] - first["win_rate"]
        pnl_change = second["avg_pnl"] - first["avg_pnl"]

        if wr_change > 0.10 and pnl_change > 0:
            trend = "improving"
        elif wr_change < -0.10 and pnl_change < 0:
            trend = "degrading"
        else:
            trend = "stable"

        return {
            "status": trend,
            "first_half": first,
            "second_half": second,
            "win_rate_change": round(wr_change, 3),
            "avg_pnl_change": round(pnl_change, 2),
        }

    # ─────────────────────────────────────────
    # Insights & Recommendations
    # ─────────────────────────────────────────

    def _generate_daily_insights(self, today: list[dict], all_trades: list[dict]) -> list[dict]:
        """Generate end-of-day insights."""
        insights = []
        pnls = [t["pnl"] for t in today if t["pnl"] is not None]

        if pnls:
            day_pnl = sum(pnls)
            wins = len([p for p in pnls if p > 0])
            total = len(pnls)

            if day_pnl > 0:
                insights.append({
                    "level": "good",
                    "message": f"Profitable day: ${day_pnl:+.2f} ({wins}/{total} wins)",
                })
            else:
                insights.append({
                    "level": "warning",
                    "message": f"Losing day: ${day_pnl:+.2f} ({wins}/{total} wins)",
                })

        # Check consecutive losses
        streak = self.journal.analyze_streaks()
        if streak["current_type"] == "loss" and streak["current_streak"] >= 3:
            insights.append({
                "level": "critical",
                "message": (
                    f"Losing streak: {streak['current_streak']} consecutive losses. "
                    "Consider reducing position size or pausing."
                ),
            })

        return insights

    def _generate_weekly_insights(self, recent: list[dict], all_trades: list[dict]) -> list[dict]:
        """Generate weekly insights."""
        insights = []

        # Day-of-week analysis
        dow = self.journal.analyze_by_day_of_week()
        if dow:
            worst_day = min(dow, key=lambda d: dow[d]["total_pnl"])
            best_day = max(dow, key=lambda d: dow[d]["total_pnl"])
            if dow[worst_day]["total_pnl"] < -100:
                insights.append({
                    "level": "warning",
                    "param": "day_of_week",
                    "message": (
                        f"Worst day: {worst_day} (${dow[worst_day]['total_pnl']:+.0f}). "
                        f"Best day: {best_day} (${dow[best_day]['total_pnl']:+.0f})."
                    ),
                })

        # Trend check
        trend = self._analyze_trends(all_trades)
        if trend.get("status") == "degrading":
            insights.append({
                "level": "critical",
                "param": "overall",
                "message": (
                    f"Performance degrading: WR changed by {trend['win_rate_change']:+.1%}, "
                    f"avg P&L changed by ${trend['avg_pnl_change']:+.2f}/trade"
                ),
            })
        elif trend.get("status") == "improving":
            insights.append({
                "level": "good",
                "param": "overall",
                "message": (
                    f"Performance improving: WR changed by {trend['win_rate_change']:+.1%}, "
                    f"avg P&L changed by ${trend['avg_pnl_change']:+.2f}/trade"
                ),
            })

        return insights

    def _generate_recommendations(self, recent: list[dict], all_trades: list[dict]) -> list[dict]:
        """Generate strategic recommendations based on all data."""
        recs = []

        # Check if any symbol should be paused
        by_sym = self.journal.analyze_by_symbol()
        for sym, data in by_sym.items():
            if data["trades"] >= 10 and data["win_rate"] < 0.30 and data["total_pnl"] < -300:
                recs.append({
                    "priority": "high",
                    "param": "enabled",
                    "symbol": sym,
                    "action": f"Consider disabling {sym}: WR={data['win_rate']:.0%}, P&L=${data['total_pnl']:+.0f}",
                })

        # Check if daily profit cap needs adjustment
        daily_pnls = self.journal.daily_pnl_breakdown()
        if daily_pnls:
            max_day = max(daily_pnls.values())
            total = sum(daily_pnls.values())
            if total > 0 and max_day / total > 0.50:
                recs.append({
                    "priority": "medium",
                    "param": "daily_profit_cap",
                    "symbol": "global",
                    "action": (
                        f"Highest day (${max_day:.0f}) is {max_day/total:.0%} of total. "
                        "Consider lowering daily cap for consistency rule."
                    ),
                })

        return recs

    # ─────────────────────────────────────────
    # Scoring
    # ─────────────────────────────────────────

    def _score_day(self, today: list[dict], all_trades: list[dict]) -> dict:
        """Score today's trading quality (0-100) across multiple dimensions."""
        pnls = [t["pnl"] for t in today if t["pnl"] is not None]
        if not pnls:
            return {"total": 0}

        # Win rate score (0-30)
        wr = len([p for p in pnls if p > 0]) / len(pnls)
        wr_score = min(30, wr * 60)  # 50% WR = 30 points

        # P&L score (0-30)
        day_pnl = sum(pnls)
        pnl_score = min(30, max(0, (day_pnl + 500) / 1000 * 30))  # -$500 = 0, +$500 = 30

        # Risk management score (0-20)
        worst_trade = min(pnls) if pnls else 0
        daily_limit = config.ACTIVE_CHALLENGE.get("daily_loss_limit", 1000)
        risk_score = 20 if worst_trade > -daily_limit * 0.3 else 10 if worst_trade > -daily_limit * 0.5 else 0

        # Discipline score (0-20): trading within limits
        disc_score = 20
        if len(pnls) > config.MAX_DAILY_TRADES * 0.8:
            disc_score -= 5  # Close to daily cap
        streak = self.journal.analyze_streaks()
        if streak["current_type"] == "loss" and streak["current_streak"] >= 3:
            disc_score -= 10  # Trading through losing streak

        total = wr_score + pnl_score + risk_score + disc_score

        return {
            "total": round(total),
            "win_rate_score": round(wr_score),
            "pnl_score": round(pnl_score),
            "risk_score": round(risk_score),
            "discipline_score": round(disc_score),
        }

    # ─────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────

    def _save_report(self, report: dict):
        """Save latest report to file."""
        try:
            tmp = REPORT_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(report, f, indent=2, default=str)
            os.replace(tmp, REPORT_FILE)
        except Exception as e:
            logger.warning("Failed to save learning report: %s", e)

    def _append_history(self, report: dict):
        """Append report summary to history for trend tracking."""
        history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE) as f:
                    history = json.load(f)
            except (json.JSONDecodeError, TypeError):
                history = []

        # Keep compact summary
        summary = {
            "type": report["type"],
            "date": report["date"],
            "trades": report.get("trades_today", report.get("trades_this_week", 0)),
            "pnl": report.get("daily_pnl", 0),
            "score": report.get("score", {}).get("total", 0),
            "adjustments": len(report.get("tuner_adjustments", [])),
            "insights_count": len(report.get("insights", [])),
        }
        history.append(summary)

        # Keep last 365 entries
        history = history[-365:]

        try:
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(history, f, indent=2, default=str)
            os.replace(tmp, HISTORY_FILE)
        except Exception as e:
            logger.warning("Failed to save learning history: %s", e)
