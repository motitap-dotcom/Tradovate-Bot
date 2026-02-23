"""
Trade Journal & Learning System
=================================
Records every trade, analyzes patterns, and generates actionable insights.

Features:
- Automatic trade logging from bot signals and fills
- Per-symbol, per-strategy, per-time-of-day performance tracking
- Win rate, profit factor, expectancy calculations
- Daily/weekly reports with lessons learned
- Strategy parameter recommendations based on historical data

Usage:
    # In bot.py — auto-logs trades:
    from trade_journal import TradeJournal
    journal = TradeJournal()
    journal.record_entry(symbol, direction, price, qty, strategy, reason)
    journal.record_exit(trade_id, exit_price, pnl, exit_reason)

    # Generate report:
    python trade_journal.py                # Full report
    python trade_journal.py --today        # Today only
    python trade_journal.py --lessons      # Key lessons & recommendations
"""

import json
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, date, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

JOURNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_journal.json")


class TradeJournal:
    """Records trades and generates performance analytics."""

    def __init__(self, filepath: str = JOURNAL_FILE):
        self.filepath = filepath
        self.trades: list[dict] = []
        self.daily_notes: dict[str, str] = {}
        self._load()

    # ─────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath) as f:
                    data = json.load(f)
                self.trades = data.get("trades", [])
                self.daily_notes = data.get("daily_notes", {})
                logger.info("Loaded %d trades from journal", len(self.trades))
            except (json.JSONDecodeError, KeyError):
                self.trades = []
                self.daily_notes = {}

    def _save(self):
        data = {
            "trades": self.trades,
            "daily_notes": self.daily_notes,
            "summary": self._compute_summary(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

    # ─────────────────────────────────────────
    # Recording
    # ─────────────────────────────────────────

    def record_entry(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        qty: int,
        strategy: str,
        reason: str,
        stop_loss: float = 0,
        take_profit: float = 0,
    ) -> str:
        """Record a new trade entry. Returns trade_id."""
        trade_id = f"{symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        trade = {
            "id": trade_id,
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "qty": qty,
            "strategy": strategy,
            "reason": reason,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_hour_et": _current_et_hour(),
            "date": date.today().isoformat(),
            "exit_price": None,
            "pnl": None,
            "exit_reason": None,
            "exit_time": None,
            "status": "open",
        }
        self.trades.append(trade)
        self._save()
        logger.info("Journal: ENTRY %s %s %s @ %.2f x%d (%s)", trade_id, direction, symbol, entry_price, qty, reason)
        return trade_id

    def record_exit(
        self,
        trade_id: str,
        exit_price: float,
        pnl: float,
        exit_reason: str = "signal",
    ):
        """Record a trade exit."""
        for trade in reversed(self.trades):
            if trade["id"] == trade_id and trade["status"] == "open":
                trade["exit_price"] = exit_price
                trade["pnl"] = pnl
                trade["exit_reason"] = exit_reason
                trade["exit_time"] = datetime.now(timezone.utc).isoformat()
                trade["status"] = "closed"

                # Calculate R-multiple
                if trade["stop_loss"] and trade["entry_price"]:
                    risk = abs(trade["entry_price"] - trade["stop_loss"]) * trade["qty"]
                    spec = config.CONTRACT_SPECS.get(trade["symbol"], {})
                    pv = spec.get("point_value", 1)
                    risk_dollars = risk * pv
                    trade["r_multiple"] = pnl / risk_dollars if risk_dollars else 0
                else:
                    trade["r_multiple"] = 0

                # Duration
                entry_dt = datetime.fromisoformat(trade["entry_time"])
                exit_dt = datetime.fromisoformat(trade["exit_time"])
                trade["duration_minutes"] = (exit_dt - entry_dt).total_seconds() / 60

                self._save()
                result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
                logger.info(
                    "Journal: EXIT %s %s P&L=$%.2f R=%.1f (%s)",
                    trade_id, result, pnl, trade["r_multiple"], exit_reason,
                )
                return

        logger.warning("Journal: trade_id %s not found for exit", trade_id)

    def record_exit_by_symbol(self, symbol: str, exit_price: float, pnl: float, exit_reason: str = "signal"):
        """Record exit for the most recent open trade on a symbol."""
        for trade in reversed(self.trades):
            if trade["symbol"] == symbol and trade["status"] == "open":
                self.record_exit(trade["id"], exit_price, pnl, exit_reason)
                return
        logger.warning("Journal: no open trade found for %s", symbol)

    # ─────────────────────────────────────────
    # Analysis
    # ─────────────────────────────────────────

    def _closed_trades(self, since: Optional[str] = None) -> list[dict]:
        """Get closed trades, optionally filtered by date."""
        trades = [t for t in self.trades if t["status"] == "closed"]
        if since:
            trades = [t for t in trades if t["date"] >= since]
        return trades

    def _compute_summary(self) -> dict:
        """Compute overall performance summary."""
        closed = self._closed_trades()
        if not closed:
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0}

        wins = [t for t in closed if t["pnl"] and t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] and t["pnl"] < 0]
        pnls = [t["pnl"] for t in closed if t["pnl"] is not None]

        avg_win = statistics.mean([t["pnl"] for t in wins]) if wins else 0
        avg_loss = statistics.mean([abs(t["pnl"]) for t in losses]) if losses else 0

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0,
            "total_pnl": sum(pnls),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": (sum(t["pnl"] for t in wins) / sum(abs(t["pnl"]) for t in losses)) if losses else 0,
            "expectancy": statistics.mean(pnls) if pnls else 0,
            "best_trade": max(pnls) if pnls else 0,
            "worst_trade": min(pnls) if pnls else 0,
            "avg_r_multiple": statistics.mean([t.get("r_multiple", 0) for t in closed if t.get("r_multiple")]) or 0,
        }

    def analyze_by_symbol(self) -> dict:
        """Performance breakdown per symbol."""
        by_sym = defaultdict(list)
        for t in self._closed_trades():
            by_sym[t["symbol"]].append(t)

        result = {}
        for sym, trades in by_sym.items():
            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            wins = [p for p in pnls if p > 0]
            result[sym] = {
                "trades": len(trades),
                "win_rate": len(wins) / len(pnls) if pnls else 0,
                "total_pnl": sum(pnls),
                "avg_pnl": statistics.mean(pnls) if pnls else 0,
            }
        return result

    def analyze_by_strategy(self) -> dict:
        """Performance breakdown per strategy type."""
        by_strat = defaultdict(list)
        for t in self._closed_trades():
            by_strat[t["strategy"]].append(t)

        result = {}
        for strat, trades in by_strat.items():
            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            wins = [p for p in pnls if p > 0]
            result[strat] = {
                "trades": len(trades),
                "win_rate": len(wins) / len(pnls) if pnls else 0,
                "total_pnl": sum(pnls),
            }
        return result

    def analyze_by_hour(self) -> dict:
        """Performance by entry hour (ET). Identifies best/worst times."""
        by_hour = defaultdict(list)
        for t in self._closed_trades():
            hour = t.get("entry_hour_et", "?")
            by_hour[hour].append(t)

        result = {}
        for hour, trades in sorted(by_hour.items()):
            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            wins = [p for p in pnls if p > 0]
            result[hour] = {
                "trades": len(trades),
                "win_rate": len(wins) / len(pnls) if pnls else 0,
                "total_pnl": sum(pnls),
            }
        return result

    def analyze_by_exit_reason(self) -> dict:
        """How trades end: TP hit, SL hit, force-close, etc."""
        by_reason = defaultdict(list)
        for t in self._closed_trades():
            reason = t.get("exit_reason", "unknown")
            by_reason[reason].append(t)

        result = {}
        for reason, trades in by_reason.items():
            pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
            result[reason] = {
                "count": len(trades),
                "total_pnl": sum(pnls),
                "avg_pnl": statistics.mean(pnls) if pnls else 0,
            }
        return result

    def generate_lessons(self) -> list[str]:
        """Generate actionable insights from trade history."""
        closed = self._closed_trades()
        if len(closed) < 3:
            return ["Not enough trades yet (need at least 3 closed trades for analysis)."]

        lessons = []
        summary = self._compute_summary()

        # Overall assessment
        if summary["win_rate"] < 0.40:
            lessons.append(
                f"Win rate is low ({summary['win_rate']:.0%}). "
                "Consider tightening entry criteria or widening stops."
            )
        elif summary["win_rate"] > 0.60:
            lessons.append(
                f"Win rate is strong ({summary['win_rate']:.0%}). "
                "Consider slightly wider take-profits to increase avg win."
            )

        # Profit factor
        if summary["profit_factor"] > 0 and summary["profit_factor"] < 1.0:
            lessons.append(
                f"Profit factor is {summary['profit_factor']:.2f} (below 1.0 = losing). "
                "Average losses are larger than average wins. Tighten stops."
            )

        # R-multiple
        if summary["avg_r_multiple"] and summary["avg_r_multiple"] < 0:
            lessons.append(
                f"Average R-multiple is negative ({summary['avg_r_multiple']:.2f}R). "
                "Risk/reward execution is poor. Let winners run longer."
            )

        # Symbol analysis
        by_sym = self.analyze_by_symbol()
        best_sym = max(by_sym, key=lambda s: by_sym[s]["total_pnl"]) if by_sym else None
        worst_sym = min(by_sym, key=lambda s: by_sym[s]["total_pnl"]) if by_sym else None
        if best_sym and worst_sym and best_sym != worst_sym:
            if by_sym[worst_sym]["total_pnl"] < -100:
                lessons.append(
                    f"{worst_sym} is the worst performer (${by_sym[worst_sym]['total_pnl']:+.0f}). "
                    f"Consider reducing position size or disabling. "
                    f"Best performer: {best_sym} (${by_sym[best_sym]['total_pnl']:+.0f})."
                )

        # Time analysis
        by_hour = self.analyze_by_hour()
        if by_hour:
            worst_hour = min(by_hour, key=lambda h: by_hour[h]["total_pnl"])
            if by_hour[worst_hour]["total_pnl"] < -100:
                lessons.append(
                    f"Avoid trading at {worst_hour}:00 ET — "
                    f"worst hour with ${by_hour[worst_hour]['total_pnl']:+.0f} P&L."
                )

        # Exit analysis
        by_exit = self.analyze_by_exit_reason()
        if "stop_loss" in by_exit and "take_profit" in by_exit:
            sl_count = by_exit["stop_loss"]["count"]
            tp_count = by_exit["take_profit"]["count"]
            total = sl_count + tp_count
            if total > 0 and sl_count / total > 0.7:
                lessons.append(
                    f"Stop losses hit {sl_count}/{total} times ({sl_count/total:.0%}). "
                    "Stops may be too tight. Consider widening by 10-20%."
                )
            elif total > 0 and tp_count / total > 0.7:
                lessons.append(
                    f"Take-profits hit {tp_count}/{total} times ({tp_count/total:.0%}). "
                    "Consider widening TP targets to capture more profit."
                )

        # Consecutive losses
        max_streak = _longest_losing_streak(closed)
        if max_streak >= 3:
            lessons.append(
                f"Longest losing streak: {max_streak} trades. "
                "Consider pausing after 3 consecutive losses."
            )

        # Duration
        durations = [t.get("duration_minutes", 0) for t in closed if t.get("duration_minutes")]
        if durations:
            avg_dur = statistics.mean(durations)
            if avg_dur < 2:
                lessons.append(
                    f"Average trade duration is very short ({avg_dur:.1f} min). "
                    "May be exiting too early. Check if TP is too tight."
                )

        if not lessons:
            lessons.append("Performance looks solid. Keep following the plan.")

        return lessons

    # ─────────────────────────────────────────
    # Reports
    # ─────────────────────────────────────────

    def print_report(self, since: Optional[str] = None):
        """Print formatted performance report."""
        closed = self._closed_trades(since)
        period = f"since {since}" if since else "all time"

        print(f"\n{'=' * 55}")
        print(f"  TRADE JOURNAL — {period}")
        print(f"{'=' * 55}")

        if not closed:
            print("  No closed trades yet.")
            print(f"{'=' * 55}\n")
            return

        summary = self._compute_summary()
        pnl = summary["total_pnl"]
        pnl_color = "\033[32m" if pnl >= 0 else "\033[31m"
        r = "\033[0m"

        print(f"  Trades:       {summary['total_trades']}")
        print(f"  Win Rate:     {summary['win_rate']:.0%} ({summary['wins']}W / {summary['losses']}L)")
        print(f"  Total P&L:    {pnl_color}${pnl:+,.2f}{r}")
        print(f"  Avg Win:      ${summary['avg_win']:+,.2f}")
        print(f"  Avg Loss:     ${summary['avg_loss']:,.2f}")
        print(f"  Profit Factor:{summary['profit_factor']:.2f}")
        print(f"  Expectancy:   ${summary['expectancy']:+,.2f}/trade")
        print(f"  Avg R:        {summary['avg_r_multiple']:+.2f}R")

        # Per symbol
        by_sym = self.analyze_by_symbol()
        if by_sym:
            print(f"\n  {'Symbol':<8} {'Trades':>6} {'WR':>6} {'P&L':>10}")
            print(f"  {'-'*34}")
            for sym, data in sorted(by_sym.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
                c = "\033[32m" if data["total_pnl"] >= 0 else "\033[31m"
                print(f"  {sym:<8} {data['trades']:>6} {data['win_rate']:>5.0%} {c}${data['total_pnl']:>+9,.2f}{r}")

        # Per strategy
        by_strat = self.analyze_by_strategy()
        if by_strat:
            print(f"\n  {'Strategy':<12} {'Trades':>6} {'WR':>6} {'P&L':>10}")
            print(f"  {'-'*38}")
            for strat, data in by_strat.items():
                c = "\033[32m" if data["total_pnl"] >= 0 else "\033[31m"
                print(f"  {strat:<12} {data['trades']:>6} {data['win_rate']:>5.0%} {c}${data['total_pnl']:>+9,.2f}{r}")

        # Lessons
        lessons = self.generate_lessons()
        if lessons:
            print(f"\n  LESSONS & RECOMMENDATIONS:")
            print(f"  {'-'*40}")
            for i, lesson in enumerate(lessons, 1):
                print(f"  {i}. {lesson}")

        print(f"\n{'=' * 55}\n")


def _current_et_hour() -> int:
    """Get current hour in Eastern Time."""
    from datetime import timezone as tz
    try:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        return datetime.now(et).hour
    except ImportError:
        # Approximate: UTC - 5 (EST) or UTC - 4 (EDT)
        utc_hour = datetime.now(tz.utc).hour
        return (utc_hour - 5) % 24


def _longest_losing_streak(trades: list[dict]) -> int:
    """Find longest consecutive losing streak."""
    max_streak = 0
    current = 0
    for t in trades:
        if t.get("pnl") is not None and t["pnl"] < 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def main():
    """CLI entry point."""
    import sys
    journal = TradeJournal()

    if "--today" in sys.argv:
        journal.print_report(since=date.today().isoformat())
    elif "--lessons" in sys.argv:
        lessons = journal.generate_lessons()
        print("\nLESSONS & RECOMMENDATIONS:")
        print("-" * 40)
        for i, lesson in enumerate(lessons, 1):
            print(f"  {i}. {lesson}")
        print()
    else:
        journal.print_report()


if __name__ == "__main__":
    main()
