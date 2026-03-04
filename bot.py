"""
Tradovate Trading Bot
======================
Multi-asset futures trading bot with prop firm risk management.
Supports ORB (indices) and VWAP momentum (commodities) strategies.
Initializes risk manager from actual account balance via API.

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
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

import config
from risk_manager import RiskManager
from strategies import create_strategy, TradeSignal, Direction
from tradovate_api import TradovateAPI, MarketDataStream, RestMarketDataPoller, YAHOO_SYMBOLS, YahooFinanceSession
from trade_journal import TradeJournal
from auto_tuner import AutoTuner
from bot_state import save_state, load_state, build_state, restore_strategies

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


class AuthenticationError(Exception):
    """Raised when all authentication attempts are exhausted."""
    pass


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

        # Active strategy instances
        self.strategies: dict[str, object] = {}

        # Track open positions per symbol to prevent contradictory orders
        # Maps symbol -> {"direction": "Buy"/"Sell", "qty": int, "order_id": int}
        self._open_positions: dict[str, dict] = {}

        # Thread lock for shared state (risk manager, trades_today, _open_positions)
        self._lock = threading.Lock()

        # Track daily trades for logging (restored from journal on startup)
        self.trades_today: list[dict] = []
        self._restore_trades_from_journal()

        # Global cooldown: minimum seconds between any two order placements
        self._min_order_gap_seconds: int = 30
        self._last_order_time: float = 0

        # Last candle timestamp per contract from warmup (to avoid replaying in poller)
        self._warmup_last_ts: dict[str, int] = {}

    def _restore_trades_from_journal(self):
        """Load today's open trades from journal so _sync_fills can close them."""
        from datetime import date as _date
        today_str = _date.today().isoformat()
        for trade in self.journal.trades:
            if trade.get("date") == today_str and trade.get("status") == "open":
                sym = trade.get("symbol", "")
                direction = trade.get("direction", "")
                qty = trade.get("qty", 0)
                self.trades_today.append({
                    "time": trade.get("entry_time", ""),
                    "symbol": sym,
                    "direction": direction,
                    "qty": qty,
                    "stop": trade.get("stop_loss", 0),
                    "target": trade.get("take_profit", 0),
                    "reason": trade.get("reason", ""),
                    "order_id": None,
                    "journal_id": trade.get("id"),
                    "_placed_at": 0,  # old trade, no grace period
                })
                # Also track in _open_positions to prevent contradictory orders
                if sym and direction:
                    self._open_positions[sym] = {
                        "direction": direction,
                        "qty": qty,
                        "journal_id": trade.get("id"),
                    }
        if self.trades_today:
            logger.info("Restored %d open trades from journal for today", len(self.trades_today))

    # ─────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────

    def start(self):
        """Initialize connections, resolve contracts, and start trading."""
        logger.info("=" * 60)
        logger.info("Tradovate Bot starting | env=%s | dry_run=%s", config.ENVIRONMENT, self.dry_run)
        logger.info("Prop firm: %s | Account size: %s", config.PROP_FIRM, config.ACTIVE_CHALLENGE["account_size"])
        logger.info("=" * 60)

        # Authenticate (retry up to 3 times with longer backoff to avoid rate limits)
        if not self.dry_run:
            auth_ok = False
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                logger.info("Authentication attempt %d/%d...", attempt, max_attempts)
                if self.api.authenticate():
                    auth_ok = True
                    break
                # Clear stale token so next attempt starts fresh
                self.api.access_token = None
                self.api.md_access_token = None
                wait = min(attempt * 30, 120)  # 30, 60, 90 — longer waits to let rate limits clear
                logger.warning(
                    "Authentication attempt %d/%d failed. Retrying in %ds...", attempt, max_attempts, wait
                )
                # Write a status file even during auth failure so monitoring can see
                self._write_auth_status(attempt)
                # Check for shutdown between attempts
                elapsed = 0
                while elapsed < wait and not _shutdown_requested:
                    time.sleep(min(10, wait - elapsed))
                    elapsed += 10
                if _shutdown_requested:
                    break
            if not auth_ok:
                logger.error("Authentication failed after %d attempts.", max_attempts)
                self._write_auth_status(max_attempts, final=True)
                raise AuthenticationError("All %d authentication attempts failed" % max_attempts)
            logger.info("Authenticated successfully (userId=%s, account=%s)",
                        self.api.user_id, self.api.account_spec)
        else:
            logger.info("DRY RUN mode — no orders will be sent")

        # Resolve front-month contracts
        self._resolve_contracts()

        # Initialize strategies
        self._init_strategies()

        # Warm up strategies with today's historical candles (builds ORB ranges + VWAP)
        self._warm_up_strategies()

        # Sync actual balance from API BEFORE main loop so the risk manager
        # uses the real SOD balance instead of the nominal account size.
        # Without this, day_pnl = (real_balance - 50000) which immediately
        # triggers either the daily loss brake or profit cap on startup.
        if not self.dry_run:
            self._init_risk_from_api()

        # Restore persisted state (trade counts, breakout flags) from previous run today
        self._restore_state()

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
    # Risk manager initialization from API
    # ─────────────────────────────────────────

    def _init_risk_from_api(self):
        """
        Fetch the real account balance from the API and initialize the risk
        manager with it.  The risk manager defaults to the nominal account
        size ($50,000) which may differ from the actual balance, causing
        day_pnl to be wildly wrong and immediately locking trading.
        """
        try:
            # Pre-check: account_id must be set for get_cash_balance to work
            if self.api.account_id is None:
                logger.error(
                    "Cannot init risk from API: account_id is None! "
                    "Check _fetch_account_id and endpoint (base_url=%s)",
                    self.api.base_url,
                )
                return

            logger.info(
                "Fetching initial balance for account_id=%s on %s...",
                self.api.account_id, self.api.base_url,
            )
            snapshot = self.api.get_cash_balance()
            if not snapshot or snapshot.get("errorText"):
                logger.warning("Could not fetch initial balance: %s", snapshot)
                return

            balance = snapshot.get("totalCashValue") or snapshot.get("netLiq")
            if balance is None:
                logger.warning("Initial balance snapshot has no value: %s", snapshot)
                return

            unrealized = snapshot.get("openPnL", 0.0)

            # Set the risk manager's baseline to the real balance
            self.risk.current_balance = balance
            self.risk.day_start_balance = balance
            self.risk.starting_balance = balance

            # Update peak if actual balance exceeds nominal
            equity = balance + unrealized
            if equity > self.risk.peak_balance:
                self.risk.peak_balance = equity
                self.risk.drawdown_floor = equity - self.risk.max_trailing_drawdown

            self.risk.unrealized_pnl = unrealized
            self.risk.day_pnl = 0.0  # Fresh start for the day

            logger.info(
                "Risk manager initialized from API | balance=%.2f | "
                "peak=%.2f | floor=%.2f | unrealized=%.2f",
                balance,
                self.risk.peak_balance,
                self.risk.drawdown_floor,
                unrealized,
            )

        except Exception as e:
            logger.error("Failed to initialize risk from API: %s", e)

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
    # State persistence (survives restarts)
    # ─────────────────────────────────────────

    def _restore_state(self):
        """Restore trade counts and cooldowns from previous run today."""
        state = load_state()
        if state is None:
            logger.info("No persisted state for today. Starting fresh.")
            return

        restore_strategies(state, self.strategies)

        # Restore daily trade count in risk manager
        saved_count = state.get("trades_today_count", 0)
        self.risk.trades_today = saved_count
        logger.info("Restored trades_today=%d from persisted state", saved_count)

    def _persist_state(self):
        """Save current strategy state to disk. Call after every trade."""
        state = build_state(
            self.strategies,
            self.risk.trades_today,
            self.trades_today,
        )
        save_state(state)

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
                yahoo = YahooFinanceSession.get()
                data = yahoo.fetch_chart(yahoo_sym)
                if data is None:
                    logger.warning("Warmup: Yahoo data unavailable for %s", yahoo_sym)
                    continue

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
        volume = data.get("trade", {}).get("size", 0) or 0

        self._process_price(symbol, price, high, low, volume)

    def _process_price(
        self, symbol: str, price: float, high: float, low: float, volume: float = 0
    ):
        """Run price through the strategy and risk manager."""
        strategy = self.strategies.get(symbol)
        if strategy is None:
            return

        # Quick pre-check (don't bother running strategy if we can't trade)
        with self._lock:
            ok, reason = self.risk.can_trade()
            if not ok:
                return
            # Skip if we already have an open position for this symbol
            if symbol in self._open_positions:
                return

        # Check time constraints
        current = now_et()
        cutoff = parse_time_et(config.TRADING_CUTOFF_ET)
        if current >= cutoff:
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
        with self._lock:
            # Global cooldown: prevent rapid-fire orders across all symbols
            elapsed = time.time() - self._last_order_time
            if elapsed < self._min_order_gap_seconds:
                logger.info(
                    "Signal for %s deferred: global cooldown (%ds remaining)",
                    signal.symbol, int(self._min_order_gap_seconds - elapsed),
                )
                return

            # Block contradictory orders: don't open opposite direction while position is open
            existing = self._open_positions.get(signal.symbol)
            if existing:
                if existing["direction"] != signal.direction.value:
                    logger.warning(
                        "Signal BLOCKED: %s %s would contradict open %s position. Skipping.",
                        signal.direction.value, signal.symbol, existing["direction"],
                    )
                    return
                else:
                    logger.info(
                        "Signal skipped: already have open %s position for %s",
                        existing["direction"], signal.symbol,
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

            # Mark position as open BEFORE placing order (prevents race with another signal)
            self._open_positions[signal.symbol] = {
                "direction": signal.direction.value,
                "qty": signal.qty,
            }

        # Place bracket order via API (outside lock — this is a slow network call)
        result = self.api.place_bracket_order(
            symbol=contract_name,
            action=signal.direction.value,
            qty=signal.qty,
            entry_price=signal.entry_price,
            stop_price=signal.stop_loss,
            take_profit_price=signal.take_profit,
            order_type="Market",
        )

        with self._lock:
            if result:
                self._last_order_time = time.time()
                self.risk.register_open(signal.qty)

                # Try to get actual fill price from the order
                fill_price = self._get_fill_price(result.get("orderId"))

                trade_id = self.journal.record_entry(
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    entry_price=fill_price or signal.entry_price or 0,
                    qty=signal.qty,
                    strategy=type(self.strategies.get(signal.symbol, "")).__name__,
                    reason=signal.reason,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                )
                self._open_positions[signal.symbol]["order_id"] = result.get("orderId")
                self._open_positions[signal.symbol]["journal_id"] = trade_id
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
                        "_placed_at": time.time(),
                    }
                )
                logger.info("Order placed: orderId=%s fill=%.2f (journal: %s)", result.get("orderId"), fill_price or 0, trade_id)

                # Persist state after every trade to survive restarts
                self._persist_state()
            else:
                logger.error("Order placement failed for %s", signal.symbol)
                # Remove the pre-reserved position since order failed
                self._open_positions.pop(signal.symbol, None)

    # ─────────────────────────────────────────
    # Fill price capture
    # ─────────────────────────────────────────

    def _get_fill_price(self, order_id: int) -> float | None:
        """
        Try to get the actual fill price for a market order.
        Market orders fill nearly instantly, but we give a brief pause.
        Returns the fill price or None if not yet filled.
        """
        if not order_id or self.dry_run:
            return None
        try:
            # Brief pause for market order to fill
            time.sleep(1)
            fills = self.api.get_fills()
            for fill in fills:
                if fill.get("orderId") == order_id:
                    return fill.get("price")
            # Also try order/item for avgFillPrice
            order = self.api._get(f"/order/item?id={order_id}")
            if order:
                return order.get("avgFillPrice") or order.get("price")
        except Exception as e:
            logger.debug("Could not get fill price for order %s: %s", order_id, e)
        return None

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
                    # Record force-close exits in journal
                    with self._lock:
                        for t in self.trades_today:
                            if t.get("journal_id") and not t.get("_closed"):
                                self.journal.record_exit_by_symbol(
                                    t["symbol"], 0, 0, exit_reason="force_close"
                                )
                                t["_closed"] = True
                        self._open_positions.clear()
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
                        logger.error(
                            "=== AUTO-RECOVERY: Re-authentication FAILED (attempt %d). "
                            "Token may be expired and password auth failing. "
                            "Bot will keep retrying every %d cycles. ===",
                            self._consecutive_api_failures,
                            _MAX_API_FAILURES_BEFORE_REAUTH,
                        )

                # Auto-fallback: if WebSocket died or stopped delivering data,
                # switch to REST polling
                if not self.dry_run and self.md_stream and isinstance(self.md_stream, MarketDataStream):
                    should_fallback = False
                    reason = ""
                    if self.md_stream.fell_back.is_set():
                        should_fallback = True
                        reason = "WebSocket unrecoverable (too many consecutive failures)"
                    elif self.md_stream.data_stale:
                        should_fallback = True
                        reason = "WebSocket stale (no data for %ds)" % self.md_stream.DATA_TIMEOUT
                    elif not self.md_stream._connected.is_set() and not self.md_stream._should_run:
                        should_fallback = True
                        reason = "WebSocket disconnected and stopped"

                    if should_fallback:
                        logger.warning("=== FALLBACK: %s. Switching to REST polling... ===", reason)
                        self.md_stream.stop()
                        poller = RestMarketDataPoller()
                        poller._last_ts.update(self._warmup_last_ts)
                        poller.start()
                        self.md_stream = poller
                        self._subscribe_market_data()
                        logger.info("=== Switched to REST market data polling ===")

                # Periodic status update (now reflects real balance)
                status = self.risk.status()
                # Identify active data source
                if isinstance(self.md_stream, MarketDataStream):
                    ds = "WS"
                    if self.md_stream._connected.is_set():
                        ds = "WS-OK"
                    elif self.md_stream.data_stale:
                        ds = "WS-STALE"
                elif self.md_stream is not None:
                    ds = "REST"
                else:
                    ds = "NONE"
                logger.info(
                    "Status | balance=%.2f | day_pnl=%.2f | to_floor=%.2f | contracts=%d/%d | trades=%d/%d | locked=%s | data=%s%s",
                    status["balance"],
                    status["day_pnl"],
                    status["distance_to_floor"],
                    status["open_contracts"],
                    status["max_contracts"],
                    status["trades_today"],
                    status["max_daily_trades"],
                    status["locked"],
                    ds,
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

        Uses the API as the authoritative source for open positions. Detects when
        a bracket order's SL/TP has been hit (position goes flat) and records the
        actual realized P&L from the account balance change.
        """
        try:
            positions = self.api.get_positions()

            # Build contractId -> base symbol mapping from our contract_map
            if not hasattr(self, "_contract_id_to_symbol"):
                self._contract_id_to_symbol = {}
            for symbol, contract_name in self.contract_map.items():
                if symbol not in [v for v in self._contract_id_to_symbol.values()]:
                    contract = self.api.find_contract(contract_name)
                    if contract:
                        self._contract_id_to_symbol[contract["id"]] = symbol

            # Count total open contracts from API (authoritative)
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

            with self._lock:
                # Set open_contracts from API (authoritative) — do NOT also call register_close
                self.risk.open_contracts = total_open

                # Close journal trades where position is now flat
                for trade_info in self.trades_today:
                    journal_id = trade_info.get("journal_id")
                    sym = trade_info.get("symbol")
                    if not journal_id or trade_info.get("_closed"):
                        continue

                    # Grace period: don't mark a trade as closed within 60s of placement.
                    # This prevents a race where _sync_fills runs before the entry order
                    # fills or before the OCO bracket is placed.
                    placed_at = trade_info.get("_placed_at", 0)
                    if placed_at and (time.time() - placed_at) < 60:
                        continue

                    if sym not in open_base_symbols:
                        # Position is flat — trade was closed (by SL/TP/manual)
                        # Try to get actual P&L from fills
                        actual_pnl = self._get_trade_pnl(trade_info)
                        exit_price = self._get_last_fill_price(trade_info)

                        self.journal.record_exit_by_symbol(
                            sym, exit_price, actual_pnl, exit_reason="bracket_fill"
                        )
                        trade_info["_closed"] = True
                        # Remove from open positions tracker
                        self._open_positions.pop(sym, None)
                        logger.info(
                            "Position closed for %s (flat) | P&L=%.2f | exit=%.2f",
                            sym, actual_pnl, exit_price,
                        )

        except Exception as e:
            logger.error("Fill sync error: %s", e)

    def _get_trade_pnl(self, trade_info: dict) -> float:
        """Try to calculate realized P&L for a closed trade from API fills."""
        try:
            fills = self.api.get_fills()
            order_id = trade_info.get("order_id")
            if not fills or not order_id:
                return 0.0

            # Find fills related to this trade's orders
            entry_fill_price = 0.0
            exit_fill_price = 0.0
            qty = trade_info.get("qty", 1)
            direction = trade_info.get("direction", "Buy")

            for fill in fills:
                if fill.get("orderId") == order_id:
                    entry_fill_price = fill.get("price", 0)

            # If we can't find fills, estimate from balance change
            if not entry_fill_price:
                return 0.0

            # Get the most recent fill for the same contract (the SL/TP exit)
            contract_name = self.contract_map.get(trade_info.get("symbol", ""))
            for fill in reversed(fills):
                foid = fill.get("orderId")
                if foid and foid != order_id:
                    exit_fill_price = fill.get("price", 0)
                    break

            if entry_fill_price and exit_fill_price:
                spec = config.CONTRACT_SPECS.get(trade_info.get("symbol", ""), {})
                pv = spec.get("point_value", 1)
                if direction == "Buy":
                    return (exit_fill_price - entry_fill_price) * qty * pv
                else:
                    return (entry_fill_price - exit_fill_price) * qty * pv

        except Exception as e:
            logger.debug("Could not calculate trade P&L: %s", e)
        return 0.0

    def _get_last_fill_price(self, trade_info: dict) -> float:
        """Get exit fill price for a closed trade."""
        try:
            fills = self.api.get_fills()
            order_id = trade_info.get("order_id")
            if not fills:
                return 0.0
            # Return the most recent fill price (likely the SL/TP fill)
            contract_name = self.contract_map.get(trade_info.get("symbol", ""))
            for fill in reversed(fills):
                foid = fill.get("orderId")
                if foid and foid != order_id:
                    return fill.get("price", 0)
        except Exception:
            pass
        return 0.0

    def _sync_balance(self):
        """Fetch latest balance from API and update risk manager."""
        try:
            if self.api.account_id is None:
                logger.warning("Balance sync skipped: account_id is None")
                return
            snapshot = self.api.get_cash_balance()
            if snapshot:
                if snapshot.get("errorText"):
                    logger.warning("Cash balance error: %s", snapshot["errorText"])
                    return
                # CashBalanceSnapshot fields: totalCashValue, netLiq, openPnL, realizedPnL
                balance = snapshot.get("totalCashValue") or snapshot.get("netLiq")
                if balance is not None:
                    unrealized = snapshot.get("openPnL", 0.0)
                    with self._lock:
                        self.risk.update_balance(balance, unrealized)
                else:
                    logger.debug("Cash balance snapshot has no totalCashValue/netLiq: %s", snapshot)
            else:
                logger.debug("get_cash_balance returned None (account_id=%s)", self.api.account_id)
        except Exception as e:
            logger.error("Balance sync error: %s", e)

    # ─────────────────────────────────────────
    # Live status file
    # ─────────────────────────────────────────

    _STATUS_FILE = Path(__file__).parent / "live_status.json"

    def _write_auth_status(self, attempt: int, final: bool = False):
        """Write minimal status during auth phase so monitoring can track progress."""
        try:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timestamp_et": now_et().isoformat(),
                "auth_phase": True,
                "auth_attempt": attempt,
                "auth_failed": final,
                "environment": config.ENVIRONMENT,
                "dry_run": self.dry_run,
            }
            tmp = self._STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._STATUS_FILE)
        except Exception:
            pass

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
                "open_positions": {sym: pos.get("direction") for sym, pos in self._open_positions.items()},
                "data_source": "websocket" if isinstance(self.md_stream, MarketDataStream) else "rest_polling",
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
    """
    Return a set of dates that are US futures market holidays (CME closed).
    Covers fixed holidays and rule-based holidays (e.g. 3rd Monday of January).
    """
    from datetime import date

    holidays = set()

    # New Year's Day (Jan 1, or observed on nearest weekday)
    nyd = date(year, 1, 1)
    if nyd.weekday() == 6:  # Sunday → observe Monday
        holidays.add(date(year, 1, 2))
    elif nyd.weekday() == 5:  # Saturday → observe Friday (prev year)
        pass  # Falls in previous year
    else:
        holidays.add(nyd)

    # MLK Day: 3rd Monday of January
    d = date(year, 1, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=2))

    # Presidents' Day: 3rd Monday of February
    d = date(year, 2, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=2))

    # Good Friday: 2 days before Easter Sunday
    # Easter calculation (Anonymous Gregorian algorithm)
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

    # Memorial Day: last Monday of May
    d = date(year, 5, 31)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    holidays.add(d)

    # Juneteenth (June 19)
    jn = date(year, 6, 19)
    if jn.weekday() == 6:
        holidays.add(date(year, 6, 20))
    elif jn.weekday() == 5:
        holidays.add(date(year, 6, 18))
    else:
        holidays.add(jn)

    # Independence Day (July 4)
    july4 = date(year, 7, 4)
    if july4.weekday() == 6:
        holidays.add(date(year, 7, 5))
    elif july4.weekday() == 5:
        holidays.add(date(year, 7, 3))
    else:
        holidays.add(july4)

    # Labor Day: 1st Monday of September
    d = date(year, 9, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d)

    # Thanksgiving: 4th Thursday of November
    d = date(year, 11, 1)
    while d.weekday() != 3:
        d += timedelta(days=1)
    holidays.add(d + timedelta(weeks=3))

    # Christmas (Dec 25)
    xmas = date(year, 12, 25)
    if xmas.weekday() == 6:
        holidays.add(date(year, 12, 26))
    elif xmas.weekday() == 5:
        holidays.add(date(year, 12, 24))
    else:
        holidays.add(xmas)

    return holidays


def _is_market_holiday(d) -> bool:
    """Check if a date is a US futures market holiday."""
    return d in _us_market_holidays(d.year)


def _next_trading_morning() -> datetime:
    """Return the next weekday at 09:25 ET, skipping weekends and US market holidays."""
    now = now_et()
    # Start from tomorrow
    candidate = now.replace(hour=9, minute=25, second=0, microsecond=0) + timedelta(days=1)
    # Skip weekends and holidays
    while candidate.weekday() >= 5 or _is_market_holiday(candidate.date()):
        candidate += timedelta(days=1)
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
        logger.info("Signal %s received. Stopping...", signum)
        _shutdown_requested = True
        if bot is not None:
            bot.running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # ── Daily loop: run bot, sleep until next trading morning, repeat ──
    bot = None
    while not _shutdown_requested:
        # Skip holidays — sleep until next trading day
        today = now_et().date()
        if _is_market_holiday(today):
            logger.info("Today %s is a US market holiday. Skipping.", today)
            wake_up = _next_trading_morning()
            sleep_seconds = (wake_up - now_et()).total_seconds()
            logger.info(
                "Next trading session: %s ET (sleeping %.0f minutes)",
                wake_up.strftime("%Y-%m-%d %H:%M"),
                sleep_seconds / 60,
            )
            while sleep_seconds > 0 and not _shutdown_requested:
                time.sleep(min(60, sleep_seconds))
                sleep_seconds -= 60
            continue

        try:
            bot = TradovateBot(dry_run=args.dry_run)
            bot.start()
        except AuthenticationError:
            # Auth failed — retry in 5 minutes, NOT next morning.
            # The server might just need time for CAPTCHA/rate-limit to clear.
            logger.error("Authentication failed. Retrying in 5 minutes...")
            retry_wait = 300  # 5 minutes
            while retry_wait > 0 and not _shutdown_requested:
                time.sleep(min(60, retry_wait))
                retry_wait -= 60
            continue  # Skip the sleep-until-next-morning logic
        except SystemExit:
            # Unexpected sys.exit — let systemd restart us
            raise
        except Exception:
            logger.exception("Unexpected error in bot session. Will retry next session.")

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

    logger.info("Bot process exiting.")


if __name__ == "__main__":
    main()
