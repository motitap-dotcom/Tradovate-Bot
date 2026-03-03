"""
Tradovate Trading Bot
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

        # Active strategy instances
        self.strategies: dict[str, object] = {}

        # Track daily trades for logging
        self.trades_today: list[dict] = []

        # Global cooldown: minimum seconds between any two order placements
        self._min_order_gap_seconds: int = 30
        self._last_order_time: float = 0

        # Last candle timestamp per contract from warmup (to avoid replaying in poller)
        self._warmup_last_ts: dict[str, int] = {}

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
                    for t in self.trades_today:
                        if t.get("journal_id"):
                            self.journal.record_exit_by_symbol(
                                t["symbol"], 0, 0, exit_reason="force_close"
                            )
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
                        logger.error("=== AUTO-RECOVERY: Re-authentication FAILED. Will retry next cycle. ===")

                # Auto-fallback: if WebSocket died, switch to REST polling
                if not self.dry_run and self.md_stream and hasattr(self.md_stream, "fell_back"):
                    if isinstance(self.md_stream, MarketDataStream) and self.md_stream.fell_back.is_set():
                        logger.warning("=== WebSocket unrecoverable. Switching to REST polling... ===")
                        self.md_stream.stop()
                        poller = RestMarketDataPoller()
                        poller._last_ts.update(self._warmup_last_ts)
                        poller.start()
                        self.md_stream = poller
                        self._subscribe_market_data()
                        logger.info("=== Switched to REST market data polling ===")

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
        """Check positions and fills to close journal trades and update contract count."""
        try:
            positions = self.api.get_positions()

            # Build contractId -> base symbol mapping from our contract_map
            # contract_map: {"NQ": "NQH6", "ES": "ESH6", ...}
            # We need to match position contractId (int) to our symbols.
            # Resolve this once and cache it.
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
            # Track which base symbols have open positions
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
                    # Position is flat for this symbol — trade was closed (by SL/TP/manual)
                    self.journal.record_exit_by_symbol(
                        sym, 0, 0, exit_reason="bracket_fill"
                    )
                    self.risk.register_close(trade_info.get("qty", 1))
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


def _next_trading_morning() -> datetime:
    """Return the next weekday at 09:25 ET (5 min before market open)."""
    now = now_et()
    # Start from tomorrow
    candidate = now.replace(hour=9, minute=25, second=0, microsecond=0) + timedelta(days=1)
    # Skip weekends: Saturday=5, Sunday=6
    while candidate.weekday() >= 5:
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
        bot = TradovateBot(dry_run=args.dry_run)
        bot.start()

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
