"""
Tradovate Trading Bot — v2.2 (quote-routing-fix)
======================
Multi-asset futures trading bot with prop firm risk management.
Supports ORB (indices) and VWAP momentum (commodities) strategies.

Deployment: Push & Flow — push to main triggers automatic server update.
No diagnostics (ping/curl/ssh) needed. (Updated 2026-02-27)

Usage:
    python bot.py              # Run in demo mode (default)
    python bot.py --live       # Run in live mode (use with caution)
    python bot.py --dry-run    # Paper mode — signals only, no orders sent
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

import config
from risk_manager import RiskManager
from strategies import create_strategy, TradeSignal, Direction
from tradovate_api import TradovateAPI, MarketDataStream, RestMarketDataPoller, YAHOO_SYMBOLS
from trade_journal import TradeJournal
from auto_tuner import AutoTuner
from continuous_learner import ContinuousLearner

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE),
    ],
)
logger = logging.getLogger("bot")


# ─────────────────────────────────────────────
# Eastern Time helper
# ─────────────────────────────────────────────

ET = ZoneInfo("America/New_York")  # Handles EST/EDT automatically


def now_et() -> datetime:
    return datetime.now(ET)


def parse_time_et(t_str: str) -> datetime:
    """Parse HH:MM string into today's datetime in ET."""
    h, m = t_str.split(":")
    today = now_et().date()
    return datetime(today.year, today.month, today.day, int(h), int(m), tzinfo=ET)


# ─────────────────────────────────────────────
# Bot class
# ─────────────────────────────────────────────


class TradovateBot:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.api = TradovateAPI()
        self.risk = RiskManager()
        self.journal = TradeJournal()
        self.md_stream: MarketDataStream = None
        self.running = False

        # Symbol → front-month contract name mapping
        self.contract_map: dict[str, str] = {}

        # Contract name → contract ID mapping (for WebSocket quote filtering)
        self.contract_ids: dict[str, int] = {}

        # Active strategy instances
        self.strategies: dict[str, object] = {}

        # Track daily trades for logging
        self.trades_today: list[dict] = []

        # Global cooldown: minimum seconds between any two order placements
        self._min_order_gap_seconds: int = 30
        self._last_order_time: float = 0

        # Last candle timestamp per contract from warmup (to avoid replaying in poller)
        self._warmup_last_ts: dict[str, int] = {}

        # Contract rollover: check every 10 minutes (not every 30s loop)
        self._last_rollover_check: float = 0
        self._rollover_check_interval: int = 600  # seconds

    # ─────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────

    def start(self):
        """Initialize connections, resolve contracts, and start trading."""
        logger.info("=" * 60)
        logger.info("Tradovate Bot starting | env=%s | dry_run=%s", config.ENVIRONMENT, self.dry_run)
        logger.info("Prop firm: %s | Account size: %s", config.PROP_FIRM, config.ACTIVE_CHALLENGE["account_size"])
        logger.info("=" * 60)

        # Authenticate (retry up to 3 times with backoff)
        if not self.dry_run:
            auth_ok = False
            for attempt in range(1, 4):
                if self.api.authenticate():
                    auth_ok = True
                    break
                wait = attempt * 10
                logger.warning(
                    "Authentication attempt %d/3 failed. Retrying in %ds...", attempt, wait
                )
                time.sleep(wait)
            if not auth_ok:
                logger.error("Authentication failed after 3 attempts. Exiting.")
                sys.exit(1)
            logger.info("Authenticated successfully")
        else:
            logger.info("DRY RUN mode — no orders will be sent")

        # Fetch real balance from API to seed risk manager correctly
        if not self.dry_run:
            self._init_balance_from_api()

        # Resolve front-month contracts
        self._resolve_contracts()

        # Initialize strategies
        self._init_strategies()

        # Warm up strategies with today's historical candles (builds ORB ranges + VWAP)
        self._warm_up_strategies()

        # Start market data stream (WebSocket preferred, REST polling fallback)
        if not self.dry_run:
            self.md_stream = self._start_market_data()
            if self.md_stream:
                self._subscribe_market_data()

        # Main loop
        self.running = True
        self._main_loop()

    def stop(self):
        """Graceful shutdown."""
        logger.info("Shutting down bot...")
        self.running = False

        if not self.dry_run:
            # Cancel all working orders
            self.api.cancel_all_orders()
            # Close all positions
            self.api.close_all_positions()

        if self.md_stream:
            self.md_stream.stop()

        self._print_summary()
        logger.info("Bot stopped.")

    def _init_balance_from_api(self):
        """Fetch actual account balance from API and seed risk manager.

        Without this, day_start_balance defaults to config account_size ($50k)
        which causes day_pnl to include ALL accumulated profit, not just today's.
        """
        try:
            snapshot = self.api.get_cash_balance()
            if snapshot and not snapshot.get("errorText"):
                # Use netLiq (net liquidation) as primary balance —
                # this matches what FundedNext displays on their dashboard
                # and includes unrealized P&L in the balance figure.
                net_liq = snapshot.get("netLiq")
                total_cash = snapshot.get("totalCashValue")
                balance = net_liq or total_cash
                if balance is not None:
                    self.risk.set_initial_balance(balance)
                    logger.info(
                        "Initial balance from API: $%.2f (netLiq=$%s, totalCash=$%s)",
                        balance, net_liq, total_cash,
                    )
                    return
            logger.warning("Could not fetch initial balance — using config default $%.2f",
                          config.ACTIVE_CHALLENGE["account_size"])
        except Exception as e:
            logger.error("Failed to fetch initial balance: %s", e)

    # ─────────────────────────────────────────
    # Contract resolution
    # ─────────────────────────────────────────

    def _resolve_contracts(self):
        """Find the front-month contract for each enabled symbol."""
        for symbol, spec in config.CONTRACT_SPECS.items():
            if not spec["enabled"]:
                logger.info("Skipping %s (disabled)", symbol)
                continue

            if self.dry_run:
                # In dry run, just use the base symbol as placeholder
                self.contract_map[symbol] = f"{symbol}__FRONT"
                logger.info("Dry run: %s -> %s", symbol, self.contract_map[symbol])
                continue

            contract = self.api.suggest_contract(symbol)
            if contract:
                contract_name = contract.get("name", symbol)
                self.contract_map[symbol] = contract_name
                contract_id = contract.get("id")
                if contract_id is not None:
                    self.contract_ids[contract_name] = contract_id
                logger.info(
                    "Resolved %s -> %s (id=%s)",
                    symbol,
                    contract_name,
                    contract_id,
                )
            else:
                logger.warning(
                    "Could not resolve front-month for %s. Skipping.", symbol
                )

    # ─────────────────────────────────────────
    # Contract rollover
    # ─────────────────────────────────────────

    @staticmethod
    def _next_liquid_contract(base_symbol: str, current_contract: str) -> str | None:
        """
        Compute the next liquid contract name based on the rollover schedule.
        E.g. for GC with current GCH6 (Mar 2026, non-liquid for gold),
        returns GCJ6 (Apr 2026, the next liquid month).

        Contract name format: <BASE><MONTH_CODE><YEAR_DIGIT>
        e.g. NQH6 = NQ + H(Mar) + 6(2026), GCJ6 = GC + J(Apr) + 6(2026)
        """
        liquid_months = config.CONTRACT_LIQUID_MONTHS.get(base_symbol)
        if not liquid_months:
            return None

        # Parse current contract: last char = year digit, second-to-last = month code
        if len(current_contract) < len(base_symbol) + 2:
            return None

        suffix = current_contract[len(base_symbol):]  # e.g. "H6"
        month_code = suffix[0]
        year_digit = int(suffix[1])

        current_month_num = config.MONTH_CODES.get(month_code)
        if current_month_num is None:
            return None

        # Current year (2-digit sense: 6 = 2026)
        current_year = year_digit

        # Find the next liquid month AFTER the current one
        # First, try remaining months in the same year
        for mc in liquid_months:
            mn = config.MONTH_CODES[mc]
            if mn > current_month_num:
                return f"{base_symbol}{mc}{current_year}"

        # Wrap to next year, take first liquid month
        next_year = (current_year + 1) % 10
        return f"{base_symbol}{liquid_months[0]}{next_year}"

    def _check_contract_rollover(self):
        """
        Check if any active contracts need to roll to the next front-month.

        Two-phase approach:
        1. DATE-BASED (proactive): If the current contract expires within
           ROLLOVER_DAYS_BEFORE_EXPIRY days, compute the next liquid contract
           from the schedule and switch immediately.
        2. SUGGEST-BASED (fallback): If Tradovate's suggest API returns a
           different contract, follow it.

        This ensures we roll early enough to avoid low-liquidity contracts
        near expiration, even when the suggest API hasn't updated yet.
        """
        if self.dry_run:
            return

        today = now_et().date()

        for symbol in list(self.contract_map.keys()):
            old_contract = self.contract_map[symbol]
            new_contract = None
            new_contract_data = None
            rollover_reason = ""

            # ── Phase 1: Date-based early rollover ──
            try:
                maturity = self.api.get_contract_maturity(old_contract)
                if maturity:
                    from datetime import date as date_type
                    if isinstance(maturity, str):
                        expiry_date = date_type.fromisoformat(maturity)
                    else:
                        expiry_date = maturity

                    days_to_expiry = (expiry_date - today).days

                    if days_to_expiry <= config.ROLLOVER_DAYS_BEFORE_EXPIRY:
                        next_name = self._next_liquid_contract(symbol, old_contract)
                        if next_name and next_name != old_contract:
                            # Verify the next contract exists on Tradovate
                            verified = self.api.find_contract(next_name)
                            if verified:
                                new_contract = next_name
                                new_contract_data = verified
                                rollover_reason = (
                                    f"expiry-based: {old_contract} expires {maturity} "
                                    f"({days_to_expiry}d away, threshold={config.ROLLOVER_DAYS_BEFORE_EXPIRY}d)"
                                )
                            else:
                                logger.warning(
                                    "Early rollover: computed %s but contract not found on Tradovate",
                                    next_name,
                                )
            except Exception as e:
                logger.warning("Date-based rollover check failed for %s: %s", symbol, e)

            # ── Phase 2: Suggest API fallback ──
            if not new_contract:
                try:
                    suggested = self.api.suggest_contract(symbol)
                    if suggested:
                        suggested_name = suggested.get("name", "")
                        if suggested_name and suggested_name != old_contract:
                            new_contract = suggested_name
                            new_contract_data = suggested
                            rollover_reason = f"suggest-api: Tradovate returned {suggested_name}"
                except Exception as e:
                    logger.warning("Suggest-based rollover check failed for %s: %s", symbol, e)

            if not new_contract:
                continue

            # ── Execute rollover ──
            logger.warning(
                "CONTRACT ROLLOVER: %s from %s -> %s (%s)",
                symbol, old_contract, new_contract, rollover_reason,
            )

            # Unsubscribe old contract from market data
            if self.md_stream:
                try:
                    self.md_stream.unsubscribe_quote(old_contract)
                except Exception as e:
                    logger.warning("Error unsubscribing %s: %s", old_contract, e)

            # Update mapping
            self.contract_map[symbol] = new_contract

            # Clear cached contract ID mapping so _sync_fills rebuilds it
            if hasattr(self, "_contract_id_to_symbol"):
                self._contract_id_to_symbol = {
                    k: v for k, v in self._contract_id_to_symbol.items()
                    if v != symbol
                }

            # Subscribe to new contract
            if self.md_stream:
                self.md_stream.subscribe_quote(
                    new_contract,
                    lambda sym, data, s=symbol: self._on_quote(s, data),
                )

            logger.info(
                "Rollover complete: %s now trading %s (id=%s)",
                symbol, new_contract, new_contract_data.get("id") if new_contract_data else "?",
            )

    # ─────────────────────────────────────────
    # Strategy initialization
    # ─────────────────────────────────────────

    def _init_strategies(self):
        """Create strategy instances for each resolved contract."""
        for symbol in self.contract_map:
            strategy = create_strategy(symbol)
            self.strategies[symbol] = strategy
            logger.info(
                "Strategy for %s: %s", symbol, type(strategy).__name__
            )

    # ─────────────────────────────────────────
    # Strategy warmup (late-start recovery)
    # ─────────────────────────────────────────

    def _warm_up_strategies(self):
        """
        Fetch today's 1-min candles from Yahoo Finance and feed them to
        strategies so they can build state (ORB ranges, VWAP levels) even
        when the bot starts after market open.  No signals are executed.
        """
        for symbol, contract_name in self.contract_map.items():
            root = contract_name[:-2] if len(contract_name) > 2 else contract_name
            yahoo_sym = YAHOO_SYMBOLS.get(root)
            if not yahoo_sym:
                continue

            strategy = self.strategies.get(symbol)
            if not strategy:
                continue

            try:
                url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
                    f"?interval=1m&range=1d"
                )
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                if resp.status_code != 200:
                    logger.warning("Warmup: Yahoo returned %d for %s", resp.status_code, yahoo_sym)
                    continue

                data = resp.json()
                result = data.get("chart", {}).get("result", [{}])[0]
                timestamps = result.get("timestamp") or []
                quotes = result.get("indicators", {}).get("quote", [{}])[0]

                highs = quotes.get("high", [])
                lows = quotes.get("low", [])
                closes = quotes.get("close", [])
                volumes = quotes.get("volume", [])

                fed = 0
                for i, ts in enumerate(timestamps):
                    c = closes[i] if i < len(closes) else None
                    h = highs[i] if i < len(highs) else None
                    l = lows[i] if i < len(lows) else None
                    v = volumes[i] if i < len(volumes) else 0

                    if c is None or h is None or l is None:
                        continue

                    candle_time = datetime.fromtimestamp(ts, tz=ET)

                    # Feed to strategy state WITHOUT executing signals
                    if hasattr(strategy, "update_vwap"):
                        # VWAP: build cumulative VWAP from per-bar data
                        strategy._current_time = candle_time
                        strategy.update_vwap(h, l, c, v or 0)
                        strategy._prev_price = c
                    else:
                        # ORB: feed candles to build the opening range
                        for window in getattr(strategy, "windows", []):
                            if not window.range_set:
                                window.feed(c, h, l, candle_time.time())
                            else:
                                # Range is set — just track _last_price so
                                # feed() can detect fresh crosses on live ticks.
                                # Do NOT mark breakout_fired here: the fresh-cross
                                # guard in feed() already prevents stale breakouts
                                # (it requires _last_price inside the range).
                                window._last_price = c

                    fed += 1

                # Remember last candle so REST poller skips replayed data
                if timestamps:
                    self._warmup_last_ts[contract_name] = timestamps[-1]

                logger.info(
                    "Warmed up %s with %d candles | strategy=%s",
                    symbol, fed, type(strategy).__name__,
                )

                # Log built ranges / VWAP
                for w in getattr(strategy, "windows", []):
                    if w.range_set:
                        logger.info(
                            "  ORB %d-min range: %.2f - %.2f (size=%.2f)",
                            w.window_minutes, w.range_low, w.range_high,
                            w.range_high - w.range_low,
                        )
                if hasattr(strategy, "vwap") and strategy.vwap:
                    logger.info("  VWAP: %.4f", strategy.vwap)

            except Exception as e:
                logger.warning("Warmup failed for %s: %s", symbol, e)

    # ─────────────────────────────────────────
    # Market data
    # ─────────────────────────────────────────

    def _start_market_data(self):
        """Try WebSocket first; fall back to REST polling if WS is unavailable."""
        if self.api.md_access_token:
            try:
                ws = MarketDataStream(self.api.md_access_token, api=self.api)
                ws.start()
                # Give it a moment to connect
                if ws._connected.wait(timeout=10):
                    logger.info("Market data via WebSocket")
                    return ws
                logger.warning("WebSocket connection failed, falling back to REST polling")
                ws.stop()
            except Exception as e:
                logger.warning("WebSocket init failed (%s), falling back to REST polling", e)

        poller = RestMarketDataPoller()
        # Seed with warmup timestamps so poller skips already-processed candles
        poller._last_ts.update(self._warmup_last_ts)
        poller.start()
        logger.info("Market data via REST polling (Yahoo Finance)")
        return poller

    def _subscribe_market_data(self):
        """Subscribe to quotes for all active symbols."""
        for symbol, contract_name in self.contract_map.items():
            contract_id = self.contract_ids.get(contract_name)
            self.md_stream.subscribe_quote(
                contract_name,
                lambda sym, data, s=symbol: self._on_quote(s, data),
                contract_id=contract_id,
            )

    def _on_quote(self, symbol: str, data: dict):
        """Handle incoming quote data from WebSocket."""
        # Extract price from quote data
        # Tradovate quote structure includes bid/ask/last
        price = data.get("trade", {}).get("price") or data.get("bid", {}).get("price")
        if price is None:
            return

        high = data.get("high", {}).get("price", price)
        low = data.get("low", {}).get("price", price)
        volume = data.get("trade", {}).get("size", 0)

        self._process_price(symbol, price, high, low, volume)

    def _process_price(
        self, symbol: str, price: float, high: float, low: float, volume: float = 0
    ):
        """Run price through the strategy and risk manager."""
        strategy = self.strategies.get(symbol)
        if strategy is None:
            return

        # Check if we can trade
        ok, reason = self.risk.can_trade()
        if not ok:
            return

        # Check time constraints — only trade within the configured window
        current = now_et()
        start = parse_time_et(config.TRADING_START_ET)
        cutoff = parse_time_et(config.TRADING_CUTOFF_ET)
        if current < start or current >= cutoff:
            return

        # Track MAE/MFE for open trades (learning data)
        self.journal.update_mae_mfe(symbol, price)

        # Feed price to strategy
        signal = None
        if hasattr(strategy, "on_price"):
            if hasattr(strategy, "update_vwap"):
                # VWAP strategy — pass current timestamp for cooldown tracking
                strategy._current_time = current
                signal = strategy.on_price(price, high, low, volume)
            else:
                # ORB strategy
                signal = strategy.on_price(price, current, high, low)

        if signal is not None:
            self._execute_signal(signal)

    # ─────────────────────────────────────────
    # Order execution
    # ─────────────────────────────────────────

    def _execute_signal(self, signal: TradeSignal):
        """Validate signal through risk manager and place bracket order."""
        # Global cooldown: prevent rapid-fire orders across all symbols
        elapsed = time.time() - self._last_order_time
        if elapsed < self._min_order_gap_seconds:
            logger.info(
                "Signal for %s deferred: global cooldown (%ds remaining)",
                signal.symbol, int(self._min_order_gap_seconds - elapsed),
            )
            return

        ok, reason = self.risk.can_trade()
        if not ok:
            logger.warning("Signal rejected by risk manager: %s", reason)
            return

        # Calculate position size
        qty = self.risk.calculate_position_size(signal.symbol)
        if qty <= 0:
            logger.warning("Position size = 0 for %s. Signal skipped.", signal.symbol)
            return
        signal.qty = qty

        contract_name = self.contract_map.get(signal.symbol)
        if not contract_name:
            logger.error("No contract mapping for %s", signal.symbol)
            return

        logger.info(
            "SIGNAL: %s %s %d @ market | SL=%.4f TP=%.4f | %s",
            signal.direction.value,
            signal.symbol,
            signal.qty,
            signal.stop_loss,
            signal.take_profit,
            signal.reason,
        )

        if self.dry_run:
            logger.info("[DRY RUN] Order would be placed: %s", signal)
            self.trades_today.append(
                {
                    "time": now_et().isoformat(),
                    "symbol": signal.symbol,
                    "direction": signal.direction.value,
                    "qty": signal.qty,
                    "stop": signal.stop_loss,
                    "target": signal.take_profit,
                    "reason": signal.reason,
                }
            )
            return

        # Place bracket order via API
        result = self.api.place_bracket_order(
            symbol=contract_name,
            action=signal.direction.value,
            qty=signal.qty,
            entry_price=signal.entry_price,
            stop_price=signal.stop_loss,
            take_profit_price=signal.take_profit,
            order_type="Market",
        )

        if result:
            self._last_order_time = time.time()
            self.risk.register_open(signal.qty)
            trade_id = self.journal.record_entry(
                symbol=signal.symbol,
                direction=signal.direction.value,
                entry_price=signal.entry_price or 0,
                qty=signal.qty,
                strategy=type(self.strategies.get(signal.symbol, "")).__name__,
                reason=signal.reason,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
            self.trades_today.append(
                {
                    "time": now_et().isoformat(),
                    "symbol": signal.symbol,
                    "direction": signal.direction.value,
                    "qty": signal.qty,
                    "stop": signal.stop_loss,
                    "target": signal.take_profit,
                    "reason": signal.reason,
                    "order_id": result.get("orderId"),
                    "journal_id": trade_id,
                }
            )
            logger.info("Order placed: orderId=%s (journal: %s)", result.get("orderId"), trade_id)

            # Fill quality tracking: check actual fill price vs signal price
            if not self.dry_run and result.get("orderId"):
                try:
                    time.sleep(0.5)
                    fill_detail = self.api._get(f"/order/item?id={result['orderId']}")
                    if fill_detail and fill_detail.get("avgPrice"):
                        avg_fill = fill_detail["avgPrice"]
                        expected = signal.entry_price or avg_fill  # Market order = no expected price
                        slippage = abs(avg_fill - expected) if signal.entry_price else 0
                        spec = config.CONTRACT_SPECS.get(signal.symbol, {})
                        slippage_dollars = slippage * spec.get("point_value", 1)
                        logger.info(
                            "FILL QUALITY: %s filled at %.4f (expected %.4f) | slippage=%.4f pts ($%.2f)",
                            signal.symbol, avg_fill, expected, slippage, slippage_dollars,
                        )
                except Exception as e:
                    logger.debug("Fill quality check error: %s", e)
        else:
            # Bug #23 fix: Do NOT leave risk manager thinking contracts are open
            # when the order actually failed. Log clearly so we can debug.
            logger.error(
                "Order placement FAILED for %s %s %d — signal discarded (risk manager NOT updated)",
                signal.direction.value, signal.symbol, signal.qty,
            )
            # Verify position state from API to avoid desync
            if not self.dry_run:
                try:
                    positions = self.api.get_positions()
                    actual_open = sum(
                        abs(p.get("netPos", 0)) for p in positions
                        if p.get("netPos", 0) != 0
                    )
                    if actual_open != self.risk.open_contracts:
                        logger.warning(
                            "Risk desync detected: risk says %d contracts, API says %d. Correcting.",
                            self.risk.open_contracts, actual_open,
                        )
                        self.risk.open_contracts = actual_open
                except Exception as e:
                    logger.warning("Position check after failed order: %s", e)

    # ─────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────

    def _main_loop(self):
        """
        Main event loop.
        In real-time mode, the WebSocket feeds drive strategy via callbacks.
        This loop handles time-based events (force close, status updates).
        """
        logger.info("Entering main loop...")

        # Track known fills to detect new ones
        self._known_fill_ids: set = set()
        # Track consecutive API failures for auto-recovery
        self._consecutive_api_failures = 0
        _MAX_API_FAILURES_BEFORE_REAUTH = 3

        while self.running:
            try:
                current = now_et()
                force_close = parse_time_et(config.FORCE_CLOSE_ET)

                # Force close all positions before session end
                if current >= force_close:
                    logger.warning("Force close time reached. Closing all positions.")
                    if not self.dry_run:
                        self.api.cancel_all_orders()
                        self.api.close_all_positions()
                        # Verify positions are actually flat (retry up to 3 times)
                        for attempt in range(1, 4):
                            time.sleep(2)
                            remaining = [
                                p for p in self.api.get_positions()
                                if p.get("netPos", 0) != 0
                            ]
                            if not remaining:
                                logger.info("Force close confirmed: all positions flat.")
                                break
                            logger.warning(
                                "Force close attempt %d: %d positions still open. Retrying...",
                                attempt, len(remaining),
                            )
                            self.api.close_all_positions()
                        else:
                            logger.critical(
                                "FORCE CLOSE FAILED: %d positions still open after 3 attempts!",
                                len(remaining),
                            )
                    # Record force-close exits in journal
                    for t in self.trades_today:
                        if t.get("journal_id"):
                            self.journal.record_exit_by_symbol(
                                t["symbol"], 0, 0, exit_reason="force_close"
                            )
                    self.risk.end_of_day_update(self.risk.current_balance)
                    # Run continuous learning system at end of day
                    try:
                        learner = ContinuousLearner(self.journal)
                        report = learner.run_daily_analysis()
                        adj_count = len(report.get("tuner_adjustments", []))
                        score = report.get("score", {}).get("total", 0)
                        logger.info(
                            "Daily learning: score=%d/100, %d adjustments, %d insights",
                            score, adj_count, len(report.get("insights", [])),
                        )
                        # Weekly analysis on Fridays
                        if now_et().weekday() == 4:  # Friday
                            weekly = learner.run_weekly_analysis()
                            logger.info(
                                "Weekly learning: %d recommendations",
                                len(weekly.get("recommendations", [])),
                            )
                    except Exception as e:
                        logger.warning("Learning system error: %s", e)
                    self.running = False
                    break

                # Update balance from API FIRST (before logging status)
                api_ok = True
                if not self.dry_run:
                    try:
                        self._sync_balance()
                        self._sync_fills()
                        self._consecutive_api_failures = 0  # Reset on success
                    except Exception as e:
                        self._consecutive_api_failures += 1
                        api_ok = False
                        logger.warning(
                            "API sync failed (%d/%d): %s",
                            self._consecutive_api_failures,
                            _MAX_API_FAILURES_BEFORE_REAUTH,
                            e,
                        )

                # Auto-recovery: re-authenticate after consecutive API failures
                if not self.dry_run and self._consecutive_api_failures >= _MAX_API_FAILURES_BEFORE_REAUTH:
                    logger.warning(
                        "=== AUTO-RECOVERY: %d consecutive API failures. Re-authenticating... ===",
                        self._consecutive_api_failures,
                    )
                    if self.api._re_authenticate():
                        logger.info("=== AUTO-RECOVERY: Re-authentication succeeded ===")
                        self._consecutive_api_failures = 0
                        # Restart market data with fresh token
                        if self.md_stream:
                            self.md_stream.stop()
                        self.md_stream = self._start_market_data()
                        if self.md_stream:
                            self._subscribe_market_data()
                    else:
                        logger.error("=== AUTO-RECOVERY: Re-authentication FAILED. Will retry next cycle. ===")

                # Market data staleness check — restart stream if no data for 2+ minutes
                if not self.dry_run and self.md_stream:
                    is_stale = getattr(self.md_stream, "data_stale", False)
                    fell_back = getattr(self.md_stream, "fell_back", None)
                    # Track consecutive staleness for escalating alerts
                    if not hasattr(self, "_md_stale_count"):
                        self._md_stale_count = 0
                    if is_stale or (fell_back and fell_back.is_set()):
                        self._md_stale_count += 1
                        reason = "fell back to REST" if (fell_back and fell_back.is_set()) else "stale data"
                        if self._md_stale_count >= 10:  # 5+ minutes of stale data
                            logger.critical(
                                "MARKET DATA DOWN for %d cycles (%s). Trading may be impaired!",
                                self._md_stale_count, reason,
                            )
                        else:
                            logger.warning(
                                "Market data stream unhealthy (%s, cycle %d). Restarting...",
                                reason, self._md_stale_count,
                            )
                        try:
                            self.md_stream.stop()
                        except Exception:
                            pass
                        self.md_stream = self._start_market_data()
                        if self.md_stream:
                            self._subscribe_market_data()
                    else:
                        if self._md_stale_count > 0:
                            logger.info("Market data recovered after %d stale cycles", self._md_stale_count)
                        self._md_stale_count = 0

                # Periodic WebSocket recovery: if currently on REST polling,
                # try to upgrade back to WebSocket every 5 minutes
                if not self.dry_run and self.md_stream and isinstance(self.md_stream, RestMarketDataPoller):
                    if not hasattr(self, "_last_ws_retry"):
                        self._last_ws_retry = time.time()
                    if time.time() - self._last_ws_retry >= 300:  # 5 minutes
                        self._last_ws_retry = time.time()
                        if self.api.md_access_token or self.api._re_authenticate():
                            logger.info("Attempting to upgrade from REST polling back to WebSocket...")
                            try:
                                ws = MarketDataStream(self.api.md_access_token, api=self.api)
                                ws.start()
                                if ws._connected.wait(timeout=10):
                                    logger.info("WebSocket recovery succeeded! Switching from REST to WebSocket.")
                                    self.md_stream.stop()
                                    self.md_stream = ws
                                    self._subscribe_market_data()
                                else:
                                    logger.info("WebSocket still unavailable, staying on REST polling.")
                                    ws.stop()
                            except Exception as e:
                                logger.warning("WebSocket recovery attempt failed: %s", e)

                # Periodic contract rollover check (every 10 min)
                if not self.dry_run and time.time() - self._last_rollover_check >= self._rollover_check_interval:
                    try:
                        self._check_contract_rollover()
                    except Exception as e:
                        logger.warning("Rollover check error: %s", e)
                    self._last_rollover_check = time.time()

                # Periodic status update (now reflects real balance)
                status = self.risk.status()
                logger.info(
                    "Status | balance=%.2f | day_pnl=%.2f | to_floor=%.2f | contracts=%d/%d | trades=%d/%d | locked=%s%s",
                    status["balance"],
                    status["day_pnl"],
                    status["distance_to_floor"],
                    status["open_contracts"],
                    status["max_contracts"],
                    status["trades_today"],
                    status["max_daily_trades"],
                    status["locked"],
                    "" if api_ok else " | API-ERROR",
                )

                # Write live status file for external monitoring
                self._write_live_status()

                time.sleep(30)  # Status update every 30 seconds

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)
                time.sleep(5)

        self.stop()

    def _sync_fills(self):
        """Check positions and fills to close journal trades and update contract count.

        Uses API positions as authoritative source. Tracks by symbol AND order ID
        to handle multiple trades on the same symbol correctly.
        """
        try:
            positions = self.api.get_positions()

            # Build contractId -> base symbol mapping from our contract_map
            if not hasattr(self, "_contract_id_to_symbol"):
                self._contract_id_to_symbol = {}
            # Lazily build the mapping from API contract lookups
            for symbol, contract_name in self.contract_map.items():
                if symbol not in [v for v in self._contract_id_to_symbol.values()]:
                    contract = self.api.find_contract(contract_name)
                    if contract:
                        self._contract_id_to_symbol[contract["id"]] = symbol

            # Count total open contracts from API (authoritative)
            total_open = 0
            # Track which base symbols have open positions and their net qty
            open_positions_by_symbol: dict[str, int] = {}
            for p in positions:
                net = abs(p.get("netPos", 0))
                if net > 0:
                    total_open += net
                    cid = p.get("contractId")
                    base_sym = self._contract_id_to_symbol.get(cid)
                    if base_sym:
                        open_positions_by_symbol[base_sym] = (
                            open_positions_by_symbol.get(base_sym, 0) + net
                        )

            # Sync authoritative contract count from API
            if total_open != self.risk.open_contracts:
                logger.debug(
                    "Contract count sync: risk=%d, API=%d",
                    self.risk.open_contracts, total_open,
                )
            self.risk.open_contracts = total_open

            # Close journal trades where position is now flat for that symbol
            for trade_info in self.trades_today:
                journal_id = trade_info.get("journal_id")
                sym = trade_info.get("symbol")
                if not journal_id or trade_info.get("_closed"):
                    continue

                if sym not in open_positions_by_symbol:
                    # Position is flat for this symbol — trade was closed (by SL/TP/manual)
                    self.journal.record_exit_by_symbol(
                        sym, 0, 0, exit_reason="bracket_fill"
                    )
                    trade_info["_closed"] = True
                    logger.info("Position closed for %s (flat)", sym)

        except Exception as e:
            logger.error("Fill sync error: %s", e)

    def _sync_balance(self):
        """Fetch latest balance from API and update risk manager."""
        try:
            snapshot = self.api.get_cash_balance()
            if snapshot:
                if snapshot.get("errorText"):
                    logger.warning("Cash balance error: %s", snapshot["errorText"])
                    return
                # Use netLiq as primary balance (matches FundedNext dashboard).
                # When using netLiq, openPnL is already baked in, so pass 0
                # for unrealized to avoid double-counting.
                net_liq = snapshot.get("netLiq")
                total_cash = snapshot.get("totalCashValue")
                open_pnl = snapshot.get("openPnL", 0.0)

                if net_liq is not None:
                    balance = net_liq
                    unrealized = 0.0  # already included in netLiq
                elif total_cash is not None:
                    balance = total_cash
                    unrealized = open_pnl  # add separately
                else:
                    logger.debug("Cash balance snapshot has no netLiq/totalCashValue: %s", snapshot)
                    return

                if not self.risk._balance_initialized:
                    logger.warning(
                        "Initial balance was never set — setting now from API: $%.2f",
                        balance,
                    )
                    self.risk.set_initial_balance(balance)
                self.risk.update_balance(balance, unrealized)
                logger.debug(
                    "Balance sync: netLiq=$%s totalCash=$%s openPnL=$%s",
                    net_liq, total_cash, open_pnl,
                )
        except Exception as e:
            logger.error("Balance sync error: %s", e)

    # ─────────────────────────────────────────
    # Live status file
    # ─────────────────────────────────────────

    _STATUS_FILE = Path(__file__).parent / "live_status.json"

    def _write_live_status(self):
        """Write current bot status to live_status.json for external monitoring."""
        try:
            status = self.risk.status()
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timestamp_et": now_et().isoformat(),
                "balance": status["balance"],
                "equity": status["equity"],
                "day_pnl": status["day_pnl"],
                "peak_balance": status["peak_balance"],
                "drawdown_floor": status["drawdown_floor"],
                "distance_to_floor": status["distance_to_floor"],
                "open_contracts": status["open_contracts"],
                "trades_today": status["trades_today"],
                "locked": status["locked"],
                "lock_reason": status["lock_reason"],
                "environment": config.ENVIRONMENT,
                "dry_run": self.dry_run,
                "active_symbols": list(self.contract_map.keys()),
                "websocket_connected": (
                    isinstance(self.md_stream, MarketDataStream)
                    and self.md_stream._connected.is_set()
                ) if self.md_stream else False,
                "market_data_source": (
                    "websocket" if isinstance(self.md_stream, MarketDataStream)
                    else "rest_polling" if isinstance(self.md_stream, RestMarketDataPoller)
                    else "none"
                ) if self.md_stream else "none",
            }
            tmp = self._STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._STATUS_FILE)
        except Exception as e:
            logger.warning("Failed to write live_status.json: %s", e)

    # ─────────────────────────────────────────
    # Reporting
    # ─────────────────────────────────────────

    def _print_summary(self):
        """Print end-of-day summary."""
        logger.info("=" * 60)
        logger.info("END OF DAY SUMMARY")
        logger.info("=" * 60)
        logger.info("Total trades: %d", len(self.trades_today))
        status = self.risk.status()
        for k, v in status.items():
            logger.info("  %s: %s", k, v)

        if self.trades_today:
            logger.info("Trades:")
            for t in self.trades_today:
                logger.info(
                    "  %s | %s %s %d | SL=%.4f TP=%.4f | %s",
                    t["time"],
                    t["direction"],
                    t["symbol"],
                    t["qty"],
                    t["stop"],
                    t["target"],
                    t["reason"],
                )


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────


def _us_market_holidays(year: int) -> set:
    """Return a set of US market holiday dates for the given year.

    Covers NYSE/CME observed holidays. Not exhaustive for every early close,
    but catches the major full-closure days to prevent wasted trading attempts.
    """
    from datetime import date
    holidays = set()
    # Fixed-date holidays (observed: Mon if Sun, Fri if Sat)
    def _observed(month, day):
        d = date(year, month, day)
        if d.weekday() == 5:  # Saturday -> Friday
            d = d - timedelta(days=1)
        elif d.weekday() == 6:  # Sunday -> Monday
            d = d + timedelta(days=1)
        return d

    holidays.add(_observed(1, 1))   # New Year's Day
    holidays.add(_observed(7, 4))   # Independence Day
    holidays.add(_observed(12, 25)) # Christmas

    # MLK Day: 3rd Monday in January
    d = date(year, 1, 1)
    mondays = 0
    while mondays < 3:
        if d.weekday() == 0:
            mondays += 1
            if mondays == 3:
                holidays.add(d)
        d += timedelta(days=1)

    # Presidents' Day: 3rd Monday in February
    d = date(year, 2, 1)
    mondays = 0
    while mondays < 3:
        if d.weekday() == 0:
            mondays += 1
            if mondays == 3:
                holidays.add(d)
        d += timedelta(days=1)

    # Good Friday: 2 days before Easter (simplified — covers most years)
    # Easter algorithm (Anonymous Gregorian)
    a = year % 19
    b = year // 100
    c = year % 100
    d_val = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d_val - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    holidays.add(easter - timedelta(days=2))  # Good Friday

    # Memorial Day: last Monday in May
    d = date(year, 5, 31)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    holidays.add(d)

    # Juneteenth: June 19
    holidays.add(_observed(6, 19))

    # Labor Day: 1st Monday in September
    d = date(year, 9, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d)

    # Thanksgiving: 4th Thursday in November
    d = date(year, 11, 1)
    thursdays = 0
    while thursdays < 4:
        if d.weekday() == 3:
            thursdays += 1
            if thursdays == 4:
                holidays.add(d)
        d += timedelta(days=1)

    return holidays


def _next_trading_morning() -> datetime:
    """Return the next weekday at 09:25 ET (5 min before market open).
    Skips weekends and US market holidays."""
    now = now_et()
    # Start from tomorrow
    candidate = now.replace(hour=9, minute=25, second=0, microsecond=0) + timedelta(days=1)
    holidays = _us_market_holidays(candidate.year)
    # Skip weekends and holidays (check next year too for Dec->Jan edge)
    while candidate.weekday() >= 5 or candidate.date() in holidays:
        candidate += timedelta(days=1)
        if candidate.date().year != now.year:
            holidays |= _us_market_holidays(candidate.year)
    return candidate


_shutdown_requested = False


def main():
    global _shutdown_requested

    parser = argparse.ArgumentParser(description="Tradovate Trading Bot")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live mode (default: demo)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Paper mode — generate signals but do not place orders",
    )
    args = parser.parse_args()

    if args.live:
        config.ENVIRONMENT = "live"
        config.REST_URL = config._URLS["live"]["rest"]
        config.WS_TRADING_URL = config._URLS["live"]["ws_trading"]
        config.WS_MARKET_URL = config._URLS["live"]["ws_market"]

    # Graceful shutdown on SIGINT / SIGTERM exits the daily loop
    def handle_signal(signum, frame):
        global _shutdown_requested
        try:
            logger.info("Signal %s received. Stopping...", signum)
        except Exception:
            pass
        _shutdown_requested = True
        try:
            if bot is not None:
                bot.running = False
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # ── Daily loop: run bot, sleep until next trading morning, repeat ──
    # The bot NEVER exits on its own — it always restarts for the next session.
    # Only SIGINT/SIGTERM (from systemd stop) will break this loop.
    bot = None
    consecutive_crashes = 0
    while not _shutdown_requested:
        try:
            bot = TradovateBot(dry_run=args.dry_run)
            bot.start()
            consecutive_crashes = 0  # Successful session resets crash counter

            if _shutdown_requested:
                break

            # Bot finished today's session — sleep until next trading morning
            wake_up = _next_trading_morning()
            sleep_seconds = (wake_up - now_et()).total_seconds()
            logger.info(
                "Session ended. Next trading session: %s ET (sleeping %.0f minutes)",
                wake_up.strftime("%Y-%m-%d %H:%M"),
                sleep_seconds / 60,
            )

            # Sleep in 60s chunks so we can respond to signals promptly
            while sleep_seconds > 0 and not _shutdown_requested:
                time.sleep(min(60, sleep_seconds))
                sleep_seconds -= 60

        except Exception as exc:
            consecutive_crashes += 1
            restart_delay = min(30 * consecutive_crashes, 300)  # 30s, 60s, ... up to 5min
            logger.critical(
                "!!! BOT CRASHED (attempt %d): %s. Restarting in %ds...",
                consecutive_crashes, exc, restart_delay,
                exc_info=True,
            )
            try:
                # Try to clean up before restart
                if bot is not None:
                    bot.running = False
                    bot.stop()
            except Exception:
                pass
            bot = None
            time.sleep(restart_delay)

    logger.info("Bot process exiting.")


if __name__ == "__main__":
    main()
