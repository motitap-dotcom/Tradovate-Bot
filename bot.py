"""
Tradovate Trading Bot
======================
Multi-asset futures trading bot with prop firm risk management.
Supports ORB (indices) and VWAP momentum (commodities) strategies.

Usage:
    python bot.py              # Run in demo mode (default)
    python bot.py --live       # Run in live mode (use with caution)
    python bot.py --dry-run    # Paper mode — signals only, no orders sent
"""

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

import config
from risk_manager import RiskManager
from strategies import create_strategy, TradeSignal, Direction
from tradovate_api import TradovateAPI, MarketDataStream, RestMarketDataPoller, YAHOO_SYMBOLS
from trade_journal import TradeJournal
from auto_tuner import AutoTuner, HourFilter

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

ET = timezone(timedelta(hours=-5))  # EST (adjust for DST as needed)


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

        # Active strategy instances
        self.strategies: dict[str, object] = {}

        # Track daily trades for logging
        self.trades_today: list[dict] = []

        # Global cooldown: minimum seconds between any two order placements
        self._min_order_gap_seconds: int = 30
        self._last_order_time: float = 0

        # Last candle timestamp per contract from warmup (to avoid replaying in poller)
        self._warmup_last_ts: dict[str, int] = {}

        # Intra-day learning: streak tracking
        self._recent_pnls: list[float] = []
        self._intraday_adjustments: list[dict] = []

        # Hour-based trade filtering
        self.hour_filter = HourFilter(self.journal)

        # Migrate stale legacy trades on startup
        self.journal.migrate_legacy_trades()

    # ─────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────

    def start(self):
        """Initialize connections, resolve contracts, and start trading."""
        logger.info("=" * 60)
        logger.info("Tradovate Bot starting | env=%s | dry_run=%s", config.ENVIRONMENT, self.dry_run)
        logger.info("Prop firm: %s | Account size: %s", config.PROP_FIRM, config.ACTIVE_CHALLENGE["account_size"])
        logger.info("=" * 60)

        # Authenticate
        if not self.dry_run:
            if not self.api.authenticate():
                logger.error("Authentication failed. Exiting.")
                sys.exit(1)
            logger.info("Authenticated successfully")
        else:
            logger.info("DRY RUN mode — no orders will be sent")

        # Resolve front-month contracts
        self._resolve_contracts()

        # Initialize strategies
        self._init_strategies()

        # Warm up strategies with today's historical candles (builds ORB ranges + VWAP)
        self._warm_up_strategies()

        # Update hour filter from recent trade data
        self.hour_filter.update()
        blocked = self.hour_filter.get_blocked()
        if blocked:
            logger.info("Hour filter active: %s", blocked)

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
                logger.info(
                    "Resolved %s -> %s (id=%s)",
                    symbol,
                    contract_name,
                    contract.get("id"),
                )
            else:
                logger.warning(
                    "Could not resolve front-month for %s. Skipping.", symbol
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
                            elif not window.breakout_fired:
                                # Range is set — check if price already broke out
                                # during warmup. Mark as fired so we don't trigger
                                # a stale breakout on the first live tick.
                                if c > window.range_high or c < window.range_low:
                                    window.breakout_fired = True
                                    logger.debug(
                                        "Warmup: consumed stale %s ORB %dm breakout at %.2f",
                                        symbol, window.window_minutes, c,
                                    )

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
                ws = MarketDataStream(self.api.md_access_token)
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
            self.md_stream.subscribe_quote(
                contract_name,
                lambda sym, data, s=symbol: self._on_quote(s, data),
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

        # Check time constraints
        current = now_et()
        cutoff = parse_time_et(config.TRADING_CUTOFF_ET)
        if current >= cutoff:
            return

        # Hour-based filter: skip consistently losing hours for this symbol
        if not self.hour_filter.is_allowed(symbol, current.hour):
            return

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
                    "sl_order_id": result.get("slOrderId"),
                    "tp_order_id": result.get("tpOrderId"),
                    "journal_id": trade_id,
                    "balance_at_entry": self.risk.current_balance,
                }
            )
            logger.info("Order placed: orderId=%s (journal: %s)", result.get("orderId"), trade_id)
        else:
            logger.error("Order placement failed for %s", signal.symbol)

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
                    # Record force-close exits in journal with actual P&L
                    time.sleep(3)  # Brief wait for fills to settle
                    for t in self.trades_today:
                        if t.get("journal_id") and not t.get("_closed"):
                            entry_fill, exit_fill, pnl, _ = self._resolve_trade_fills(t)
                            self.journal.record_exit_by_symbol(
                                t["symbol"], exit_fill, pnl, exit_reason="force_close"
                            )
                            if entry_fill or exit_fill:
                                self.journal.update_fill_data(
                                    t["journal_id"],
                                    actual_entry=entry_fill,
                                    actual_exit=exit_fill,
                                )
                            t["_closed"] = True
                    self.risk.end_of_day_update(self.risk.current_balance)
                    # Run auto-tuner at end of day
                    try:
                        tuner = AutoTuner(self.journal)
                        adjustments = tuner.run()
                        if adjustments:
                            logger.info("Auto-tuner made %d adjustments for next session", len(adjustments))
                    except Exception as e:
                        logger.warning("Auto-tuner error: %s", e)
                    self.running = False
                    break

                # Update balance from API FIRST (before logging status)
                if not self.dry_run:
                    self._sync_balance()
                    self._sync_fills()

                # Periodic status update (now reflects real balance)
                status = self.risk.status()
                logger.info(
                    "Status | balance=%.2f | day_pnl=%.2f | to_floor=%.2f | contracts=%d/%d | trades=%d/%d | locked=%s",
                    status["balance"],
                    status["day_pnl"],
                    status["distance_to_floor"],
                    status["open_contracts"],
                    status["max_contracts"],
                    status["trades_today"],
                    status["max_daily_trades"],
                    status["locked"],
                )

                time.sleep(30)  # Status update every 30 seconds

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)
                time.sleep(5)

        self.stop()

    def _sync_fills(self):
        """Check positions and fills to close journal trades with actual P&L data."""
        try:
            positions = self.api.get_positions()

            # Build contractId -> base symbol mapping (cached)
            if not hasattr(self, "_contract_id_to_symbol"):
                self._contract_id_to_symbol = {}
            for symbol, contract_name in self.contract_map.items():
                if symbol not in [v for v in self._contract_id_to_symbol.values()]:
                    contract = self.api.find_contract(contract_name)
                    if contract:
                        self._contract_id_to_symbol[contract["id"]] = symbol

            # Count open contracts from API (authoritative)
            total_open = 0
            open_base_symbols = set()
            for p in positions:
                net = abs(p.get("netPos", 0))
                if net > 0:
                    total_open += net
                    cid = p.get("contractId")
                    base_sym = self._contract_id_to_symbol.get(cid)
                    if base_sym:
                        open_base_symbols.add(base_sym)

            self.risk.open_contracts = total_open

            # Close journal trades where position is now flat
            for trade_info in self.trades_today:
                journal_id = trade_info.get("journal_id")
                sym = trade_info.get("symbol")
                if not journal_id or trade_info.get("_closed"):
                    continue

                if sym not in open_base_symbols:
                    # Position flat — resolve actual fill data
                    entry_fill, exit_fill, actual_pnl, exit_reason = \
                        self._resolve_trade_fills(trade_info)

                    self.journal.record_exit_by_symbol(
                        sym, exit_fill, actual_pnl, exit_reason=exit_reason
                    )
                    # Backfill actual fill prices for slippage tracking
                    if entry_fill or exit_fill:
                        self.journal.update_fill_data(
                            journal_id,
                            actual_entry=entry_fill,
                            actual_exit=exit_fill,
                        )

                    self.risk.register_close(trade_info.get("qty", 1))
                    trade_info["_closed"] = True
                    logger.info(
                        "Position closed for %s | P&L=$%.2f exit=%.2f (%s)",
                        sym, actual_pnl, exit_fill, exit_reason,
                    )

                    # Intra-day learning callback
                    self._on_trade_closed(trade_info, actual_pnl)

        except Exception as e:
            logger.error("Fill sync error: %s", e)

    def _resolve_trade_fills(self, trade_info: dict) -> tuple:
        """Resolve actual fill prices and P&L for a closed trade.
        Returns: (entry_fill_price, exit_fill_price, actual_pnl, exit_reason)
        """
        sym = trade_info["symbol"]
        spec = config.CONTRACT_SPECS.get(sym, {})
        point_value = spec.get("point_value", 1)
        qty = trade_info.get("qty", 1)
        direction = trade_info.get("direction", "Buy")
        dir_mult = 1 if direction == "Buy" else -1

        entry_order_id = trade_info.get("order_id")
        sl_order_id = trade_info.get("sl_order_id")
        tp_order_id = trade_info.get("tp_order_id")

        entry_fill_price = 0.0
        exit_fill_price = 0.0
        exit_reason = "bracket_fill"

        # Resolve entry fill price
        if entry_order_id:
            try:
                entry_fills = self.api.get_fills_for_order(entry_order_id)
                if entry_fills:
                    total_qty = sum(f.get("qty", 0) for f in entry_fills)
                    if total_qty > 0:
                        entry_fill_price = sum(
                            f.get("price", 0) * f.get("qty", 0) for f in entry_fills
                        ) / total_qty
            except Exception as e:
                logger.debug("Entry fill lookup failed: %s", e)

        # Resolve exit fill price — check SL first, then TP
        for oid, reason in [(sl_order_id, "stop_loss"), (tp_order_id, "take_profit")]:
            if not oid:
                continue
            try:
                exit_fills = self.api.get_fills_for_order(oid)
                if exit_fills:
                    total_qty = sum(f.get("qty", 0) for f in exit_fills)
                    if total_qty > 0:
                        exit_fill_price = sum(
                            f.get("price", 0) * f.get("qty", 0) for f in exit_fills
                        ) / total_qty
                        exit_reason = reason
                        break
            except Exception as e:
                logger.debug("Exit fill lookup for %s failed: %s", reason, e)

        # Calculate P&L from fills
        if entry_fill_price and exit_fill_price:
            actual_pnl = (exit_fill_price - entry_fill_price) * dir_mult * qty * point_value
        else:
            # Fallback: use balance difference
            actual_pnl = self._pnl_from_balance(trade_info)

        return entry_fill_price, exit_fill_price, round(actual_pnl, 2), exit_reason

    def _pnl_from_balance(self, trade_info: dict) -> float:
        """Fallback P&L estimation from balance change since trade entry."""
        balance_at_entry = trade_info.get("balance_at_entry", 0)
        if balance_at_entry and self.risk.current_balance:
            return self.risk.current_balance - balance_at_entry
        return 0.0

    # ─────────────────────────────────────────
    # Intra-day learning
    # ─────────────────────────────────────────

    def _on_trade_closed(self, trade_info: dict, actual_pnl: float):
        """Called immediately after each trade closes. Detects streaks and adapts."""
        self._recent_pnls.append(actual_pnl)

        # Count consecutive losses (from most recent)
        consecutive_losses = 0
        for p in reversed(self._recent_pnls):
            if p < 0:
                consecutive_losses += 1
            else:
                break

        # Cold streak: 3+ consecutive losses → increase conservatism
        if consecutive_losses >= 3:
            logger.warning(
                "COLD STREAK: %d consecutive losses (last 5: %s). Tightening parameters.",
                consecutive_losses,
                [f"${p:+.0f}" for p in self._recent_pnls[-5:]],
            )
            self._apply_streak_adjustments(conservative=True)

        # Update hour filter after each trade for freshest data
        self.hour_filter.update()

    def _apply_streak_adjustments(self, conservative: bool):
        """Temporarily adjust parameters based on intra-day streaks.
        Safety rule: intra-day adjustments can ONLY make the bot more conservative."""
        if not conservative:
            return  # Never increase aggressiveness intra-day

        for sym in self.contract_map:
            spec = config.CONTRACT_SPECS.get(sym)
            if not spec:
                continue

            # Widen cooldowns by 50%
            for param in ("orb_cooldown_minutes", "vwap_cooldown_minutes"):
                if param in spec:
                    old = spec[param]
                    new = int(old * 1.5)
                    spec[param] = new
                    self._intraday_adjustments.append({
                        "param": param, "symbol": sym,
                        "old_value": old, "new_value": new,
                        "reason": "cold_streak",
                    })

        # Widen global order gap
        old_gap = self._min_order_gap_seconds
        self._min_order_gap_seconds = int(old_gap * 1.5)
        self._intraday_adjustments.append({
            "param": "_min_order_gap_seconds", "symbol": "global",
            "old_value": old_gap, "new_value": self._min_order_gap_seconds,
            "reason": "cold_streak",
        })

        logger.info("Streak adjustments applied: %d changes", len(self._intraday_adjustments))

    def _sync_balance(self):
        """Fetch latest balance from API and update risk manager."""
        try:
            snapshot = self.api.get_cash_balance()
            if snapshot:
                if snapshot.get("errorText"):
                    logger.warning("Cash balance error: %s", snapshot["errorText"])
                    return
                # CashBalanceSnapshot fields: totalCashValue, netLiq, openPnL, realizedPnL
                balance = snapshot.get("totalCashValue") or snapshot.get("netLiq")
                if balance is not None:
                    unrealized = snapshot.get("openPnL", 0.0)
                    self.risk.update_balance(balance, unrealized)
                else:
                    logger.debug("Cash balance snapshot has no totalCashValue/netLiq: %s", snapshot)
        except Exception as e:
            logger.error("Balance sync error: %s", e)

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


def main():
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

    bot = TradovateBot(dry_run=args.dry_run)

    # Graceful shutdown on SIGINT / SIGTERM
    def handle_signal(signum, frame):
        logger.info("Signal %s received. Stopping...", signum)
        bot.running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bot.start()


if __name__ == "__main__":
    main()
