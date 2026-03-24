"""
Tradovate Trading Bot — v2.3.1 (journal-fix)
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
from bot_commands import read_pending_command, execute_command
from bot_state import load_state, save_state, build_state, restore_risk, restore_strategies

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
        # Symbol → contract ID mapping (for WebSocket quote routing)
        self.contract_id_map: dict[str, int] = {}

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

        # Restore saved state from today (before API balance, so day_start_balance is preserved)
        self._saved_state = load_state()
        if self._saved_state:
            restore_risk(self._saved_state, self.risk)
            logger.info("Restored risk state from saved bot_state.json")

        # Fetch real balance from API to seed risk manager correctly
        if not self.dry_run:
            self._init_balance_from_api()

        # Resolve front-month contracts
        self._resolve_contracts()

        # Initialize strategies
        self._init_strategies()

        # Restore strategy state (trade counts, cooldowns) after init
        if self._saved_state:
            restore_strategies(self._saved_state, self.strategies)
            self.risk.trades_today = self._saved_state.get("trades_today_count", 0)
            logger.info("Restored strategy state: trades_today=%d", self.risk.trades_today)

        # Warm up strategies with today's historical candles (builds ORB ranges + VWAP)
        self._warm_up_strategies()

        # Validate order placement permissions before entering main loop
        if not self.dry_run:
            self._validate_order_permissions()

        # Start market data stream (WebSocket preferred, REST polling fallback)
        if not self.dry_run:
            self.md_stream = self._start_market_data()
            if self.md_stream:
                self._subscribe_market_data()
                # Verify quotes actually flow within 90 seconds
                self._verify_market_data()

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

    def _validate_order_permissions(self):
        """Validate that the account can place orders by doing a dry API check.

        Places a far-out-of-money limit order and immediately cancels it.
        This catches auth/permission issues early instead of during live trading.
        """
        # Pick the first resolved contract for validation
        if not self.contract_map:
            logger.warning("No contracts resolved — skipping order validation")
            return

        first_sym = next(iter(self.contract_map))
        test_symbol = self.contract_map[first_sym]
        test_contract_id = self.contract_id_map.get(first_sym)
        logger.info(
            "Validating order permissions with %s (id=%s)...", test_symbol, test_contract_id
        )

        # Use a limit buy at $0.25 — will never fill, just tests the API path
        payload = {
            "accountSpec": self.api.account_spec,
            "accountId": self.api.account_id,
            "action": "Buy",
            "symbol": test_symbol,
            "orderQty": 1,
            "orderType": "Limit",
            "price": 0.25,  # trivially low — will not fill
            "timeInForce": "Day",
            "isAutomated": True,
        }
        if test_contract_id is not None:
            payload["contractId"] = test_contract_id
        try:
            result = self.api._post("/order/placeorder", payload)
            if result and "orderId" in result:
                order_id = result["orderId"]
                status = result.get("ordStatus", "?")
                logger.info(
                    "Order validation OK: orderId=%s status=%s — cancelling test order",
                    order_id, status,
                )
                # Cancel immediately
                self.api._post("/order/cancelorder", {"orderId": order_id})
            elif result and result.get("ordStatus") == "Rejected":
                reject = result.get("rejectReason", result.get("text", "unknown"))
                logger.warning(
                    "Order validation: test order REJECTED (%s). "
                    "This may indicate account restrictions — live orders might fail!",
                    reject,
                )
            else:
                logger.warning(
                    "Order validation: unexpected response: %s. "
                    "Live orders may fail — check account permissions.",
                    result,
                )
        except Exception as e:
            logger.warning("Order validation failed with exception: %s", e)

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
                    self.contract_id_map[symbol] = contract_id
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
                    logger.warning(
                        "Warmup: Yahoo returned %d for %s — ORB will use late-start from live data",
                        resp.status_code, yahoo_sym,
                    )
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

                if fed > 0:
                    logger.info(
                        "Warmed up %s with %d candles | strategy=%s",
                        symbol, fed, type(strategy).__name__,
                    )
                else:
                    logger.warning(
                        "Warmup: 0 candles for %s (%s) — ORB will use late-start from live data",
                        symbol, yahoo_sym,
                    )

                # After warmup, reset ORB state so breakout detection can fire
                # on real-time ticks. Without this:
                # 1. _last_price is the last historical close (outside range) →
                #    fresh-cross guard blocks breakouts
                # 2. breakout_fired may be True from warmup feed() which both
                #    builds range AND detects breakout on the same call
                spec = config.CONTRACT_SPECS.get(symbol, {})
                max_range = spec.get("stop_loss_points", 25) * 2
                for w in getattr(strategy, "windows", []):
                    if w.range_set:
                        range_size = w.range_high - w.range_low
                        if range_size > max_range:
                            # Range too wide for current conditions — reset so
                            # late-start builder creates a tight range from live data
                            logger.warning(
                                "  ORB %d-min range %.2f-%.2f (size=%.2f) TOO WIDE (max=%.2f). "
                                "Resetting for late-start rebuild from live ticks.",
                                w.window_minutes, w.range_low, w.range_high,
                                range_size, max_range,
                            )
                            w.range_set = False
                            w.range_high = None
                            w.range_low = None
                            w.prices = []
                            w._last_price = None
                            w.breakout_fired = False
                        else:
                            was_fired = w.breakout_fired
                            w._last_price = None
                            w.breakout_fired = False
                            logger.info(
                                "  ORB %d-min range: %.2f - %.2f (size=%.2f) — armed for breakout%s",
                                w.window_minutes, w.range_low, w.range_high,
                                range_size,
                                " (was fired during warmup, re-armed)" if was_fired else "",
                            )
                if hasattr(strategy, "vwap") and strategy.vwap:
                    # Save last warmup price BEFORE resetting, for diagnostics
                    last_warmup_price = strategy._prev_price
                    side = "above" if (last_warmup_price or 0) >= strategy.vwap else "below"
                    # Reset _prev_price to None so first real-time tick sets it,
                    # allowing crossover detection on the second tick.
                    strategy._prev_price = None
                    candle_count = getattr(strategy, "_candle_count", 0)
                    strategy._candle_count = max(candle_count, strategy.MIN_CANDLES_FOR_SIGNAL)
                    logger.info(
                        "  VWAP: %.4f — armed for crossover (last warmup price=%.4f, %s)",
                        strategy.vwap, last_warmup_price or 0, side,
                    )

            except Exception as e:
                logger.warning(
                    "Warmup failed for %s: %s — ORB will use late-start from live data",
                    symbol, e,
                )

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

    def _verify_market_data(self):
        """Verify quotes are actually flowing after subscription.

        WebSocket may connect and auth successfully but still not deliver quotes
        (observed after systemd restart). If no quotes arrive within 90 seconds,
        force switch to REST polling which uses Yahoo Finance (proven reliable).
        """
        if isinstance(self.md_stream, RestMarketDataPoller):
            return  # REST poller doesn't need verification

        logger.info("Verifying market data flow (waiting up to 90s for first quote)...")
        for i in range(18):  # 18 × 5s = 90s
            time.sleep(5)
            quotes = getattr(self.md_stream, "_quotes_received", 0)
            if quotes > 0:
                logger.info("Market data verified: %d quotes received in %ds", quotes, (i + 1) * 5)
                return

        # No quotes after 90 seconds — WebSocket is broken, force REST fallback
        logger.warning(
            "WebSocket delivered 0 quotes in 90s despite successful auth+subscribe. "
            "Forcing switch to REST polling (Yahoo Finance)."
        )
        try:
            self.md_stream.stop()
        except Exception:
            pass

        poller = RestMarketDataPoller()
        poller._last_ts.update(self._warmup_last_ts)
        poller.start()
        self.md_stream = poller
        self._subscribe_market_data()
        logger.info("Switched to REST polling (Yahoo Finance) as fallback")

    def _subscribe_market_data(self):
        """Subscribe to quotes for all active symbols."""
        for symbol, contract_name in self.contract_map.items():
            contract_id = self.contract_id_map.get(symbol)
            self.md_stream.subscribe_quote(
                contract_name,
                lambda sym, data, s=symbol: self._on_quote(s, data),
                contract_id=contract_id,
            )
            if contract_id:
                logger.info("Mapped contractId %s -> %s for quote routing", contract_id, contract_name)

    def _on_quote(self, symbol: str, data: dict):
        """Handle incoming quote data from WebSocket."""
        # Tradovate WS quote structure: {entries: {Trade: {price, size}, Bid: {price}, ...}}
        entries = data.get("entries", {})
        if entries:
            # Standard Tradovate quote with entries dict
            trade = entries.get("Trade", {})
            bid = entries.get("Bid", {})
            price = trade.get("price") or bid.get("price")
            high = entries.get("HighPrice", {}).get("price", price) if price else None
            low = entries.get("LowPrice", {}).get("price", price) if price else None
            volume = trade.get("size", 0)
        else:
            # Fallback: flat structure (e.g. REST poller or legacy format)
            price = data.get("trade", {}).get("price") or data.get("bid", {}).get("price")
            high = data.get("high", {}).get("price", price) if price else None
            low = data.get("low", {}).get("price", price) if price else None
            volume = data.get("trade", {}).get("size", 0)

        if price is None:
            return

        # Periodic quote logging: log every 100th quote per symbol for diagnostics
        count_key = f"_quote_count_{symbol}"
        count = getattr(self, count_key, 0) + 1
        setattr(self, count_key, count)
        if count <= 3 or count % 100 == 0:
            logger.info(
                "QUOTE %s #%d: price=%.4f high=%.4f low=%.4f vol=%s",
                symbol, count, price, high, low, volume,
            )

        self._process_price(symbol, price, high, low, volume)

    def _process_price(
        self, symbol: str, price: float, high: float, low: float, volume: float = 0
    ):
        """Run price through the strategy and risk manager."""
        # Manage open trade stops (breakeven + trailing) on every tick
        self._manage_trade_stops(symbol, price)

        strategy = self.strategies.get(symbol)
        if strategy is None:
            return

        current = now_et()

        # Always feed price to strategy so it tracks state (crossovers, ranges).
        # Trading gates only prevent order execution, not strategy updates.
        signal = None
        if hasattr(strategy, "on_price"):
            if hasattr(strategy, "update_vwap"):
                strategy._current_time = current
                signal = strategy.on_price(price, high, low, volume)
            else:
                signal = strategy.on_price(price, current, high, low)

        if signal is None:
            return

        logger.info(
            "SIGNAL GENERATED: %s %s | SL=%.4f TP=%.4f | %s",
            signal.direction.value, signal.symbol,
            signal.stop_loss, signal.take_profit, signal.reason,
        )
        # Persist signal to live_status for monitoring
        if not hasattr(self, "_signals_log"):
            self._signals_log = []
        self._signals_log.append({
            "time": current.isoformat(),
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "reason": signal.reason,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
        })

        # Check time constraints — only trade within the configured window
        start = parse_time_et(config.TRADING_START_ET)
        cutoff = parse_time_et(config.TRADING_CUTOFF_ET)
        if current < start or current >= cutoff:
            self._signals_log[-1]["blocked"] = f"outside trading window ({current.strftime('%H:%M')})"
            logger.info("Signal blocked: outside trading window (%s)", current.strftime("%H:%M"))
            return

        # Check if we can trade
        ok, reason = self.risk.can_trade()
        if not ok:
            self._signals_log[-1]["blocked"] = f"risk manager: {reason}"
            logger.warning("Signal blocked by risk manager: %s", reason)
            return

        self._execute_signal(signal)

    # ─────────────────────────────────────────
    # Order execution
    # ─────────────────────────────────────────

    def _execute_signal(self, signal: TradeSignal):
        """Validate signal through risk manager and place bracket order."""
        sig_entry = self._signals_log[-1] if hasattr(self, "_signals_log") and self._signals_log else {}

        # Global cooldown: prevent rapid-fire orders across all symbols
        elapsed = time.time() - self._last_order_time
        if elapsed < self._min_order_gap_seconds:
            sig_entry["blocked"] = f"cooldown ({int(self._min_order_gap_seconds - elapsed)}s left)"
            logger.info(
                "Signal for %s deferred: global cooldown (%ds remaining)",
                signal.symbol, int(self._min_order_gap_seconds - elapsed),
            )
            return

        ok, reason = self.risk.can_trade()
        if not ok:
            sig_entry["blocked"] = f"risk: {reason}"
            logger.warning("Signal rejected by risk manager: %s", reason)
            return

        # Calculate position size
        qty = self.risk.calculate_position_size(signal.symbol)
        if qty <= 0:
            sig_entry["blocked"] = "position_size=0"
            logger.warning("Position size = 0 for %s. Signal skipped.", signal.symbol)
            return
        signal.qty = qty

        contract_name = self.contract_map.get(signal.symbol)
        if not contract_name:
            sig_entry["blocked"] = "no_contract_mapping"
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
            sig_entry["blocked"] = "dry_run"
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

        # Place bracket order via API (use contractId when available for reliability)
        contract_id = self.contract_id_map.get(signal.symbol)
        result = self.api.place_bracket_order(
            symbol=contract_name,
            action=signal.direction.value,
            qty=signal.qty,
            entry_price=signal.entry_price,
            stop_price=signal.stop_loss,
            take_profit_price=signal.take_profit,
            order_type="Market",
            contract_id=contract_id,
        )

        if result:
            self._last_order_time = time.time()
            self.risk.register_open(signal.qty, symbol=signal.symbol)
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
            fill_price = result.get("fillPrice", 0) or (signal.entry_price or 0)
            # If fill_price is still 0, retry once after a brief delay (async fill)
            if not fill_price and result.get("orderId"):
                time.sleep(2)
                try:
                    detail = self.api.get_order_detail(result["orderId"])
                    if detail and detail.get("avgPrice"):
                        fill_price = detail["avgPrice"]
                except Exception:
                    pass
            # Patch journal entry with actual fill price (market orders start with 0)
            if fill_price and trade_id:
                self.journal.patch_entry_price(signal.symbol, fill_price)
            stop_distance = abs(fill_price - signal.stop_loss) if fill_price else signal.stop_loss
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
                    "fill_price": fill_price,
                    "stop_distance": stop_distance,
                    "journal_id": trade_id,
                    # Trailing stop state
                    "breakeven_done": False,
                    "current_sl": signal.stop_loss,
                    "best_r": 0.0,  # highest R-multiple seen
                }
            )
            sig_entry["executed"] = f"orderId={result.get('orderId')}"
            logger.info("Order placed: orderId=%s (journal: %s)", result.get("orderId"), trade_id)
            # Persist state after every trade so mid-day restarts don't lose context
            self._persist_state()
        else:
            err_detail = getattr(self.api, "_last_order_error", "unknown")
            sig_entry["blocked"] = f"order_failed(contract={contract_name}, acct={self.api.account_id}): {err_detail}"
            logger.error(
                "Order placement FAILED for %s %s %d (contract=%s, account_id=%s): %s",
                signal.direction.value, signal.symbol, signal.qty,
                contract_name, self.api.account_id, err_detail,
            )

    def _persist_state(self):
        """Save current bot state to disk for crash recovery."""
        try:
            state = build_state(
                strategies=self.strategies,
                trades_today_count=self.risk.trades_today,
                trades_today_list=self.trades_today,
                risk_manager=self.risk,
            )
            save_state(state)
        except Exception as e:
            logger.error("Failed to persist state: %s", e)

    # ─────────────────────────────────────────
    # Breakeven & Trailing Stop Management
    # ─────────────────────────────────────────

    def _manage_trade_stops(self, symbol: str, price: float):
        """Check open trades for this symbol and move SL if needed.

        Called on every price tick for the given symbol.
        - After +1R: move SL to breakeven (entry price)
        - After that: trail SL every additional 0.5R of favorable movement
        """
        if self.dry_run:
            return

        be_threshold = config.BREAKEVEN_R_THRESHOLD
        trail_step = config.TRAILING_STOP_STEP_R

        for trade in self.trades_today:
            if trade.get("_closed"):
                continue
            if trade.get("symbol") != symbol:
                continue

            sl_order_id = trade.get("sl_order_id")
            fill_price = trade.get("fill_price", 0)
            stop_dist = trade.get("stop_distance", 0)

            if not sl_order_id or not fill_price or stop_dist <= 0:
                continue

            # Calculate current R-multiple
            direction = trade.get("direction")
            if direction == "Buy":
                current_r = (price - fill_price) / stop_dist
            else:  # Sell
                current_r = (fill_price - price) / stop_dist

            # Track best R seen
            if current_r > trade.get("best_r", 0):
                trade["best_r"] = current_r

            best_r = trade["best_r"]

            # --- Breakeven ---
            if not trade.get("breakeven_done") and best_r >= be_threshold:
                # Move SL to entry price (breakeven)
                new_sl = fill_price
                result = self.api.modify_order(sl_order_id, new_sl)
                if result:
                    trade["breakeven_done"] = True
                    trade["current_sl"] = new_sl
                    logger.info(
                        "BREAKEVEN: %s %s | SL moved to %.4f (entry) | R=%.2f",
                        symbol, direction, new_sl, current_r,
                    )
                continue  # don't trail on the same tick we hit breakeven

            # --- Trailing stop ---
            if trade.get("breakeven_done") and best_r >= be_threshold + trail_step:
                # Calculate where SL should be based on best_r
                # Trail SL in discrete R-steps behind the peak
                r_steps_above_be = int((best_r - be_threshold) / trail_step)
                if r_steps_above_be <= 0:
                    continue

                # Move SL to (entry + r_steps * trail_step * stop_dist) for longs
                trail_r = be_threshold + (r_steps_above_be - 1) * trail_step
                if direction == "Buy":
                    new_sl = fill_price + trail_r * stop_dist
                else:
                    new_sl = fill_price - trail_r * stop_dist

                current_sl = trade.get("current_sl", 0)
                # Only move SL in favorable direction
                if direction == "Buy" and new_sl <= current_sl:
                    continue
                if direction == "Sell" and new_sl >= current_sl:
                    continue

                # Round to tick size
                spec = config.CONTRACT_SPECS.get(symbol, {})
                tick = spec.get("tick_size", 0.01)
                if direction == "Buy":
                    new_sl = round(new_sl / tick) * tick  # round to nearest tick
                else:
                    new_sl = round(new_sl / tick) * tick

                result = self.api.modify_order(sl_order_id, new_sl)
                if result:
                    trade["current_sl"] = new_sl
                    logger.info(
                        "TRAILING: %s %s | SL moved to %.4f (+%.1fR) | best_r=%.2f current_r=%.2f",
                        symbol, direction, new_sl, trail_r, best_r, current_r,
                    )

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
                    # Record force-close exits in journal + update risk manager
                    for t in self.trades_today:
                        if t.get("journal_id") and not t.get("_closed"):
                            exit_price, pnl, _ = self._resolve_exit_fill(t)
                            self.journal.record_exit_by_symbol(
                                t["symbol"], exit_price, pnl, exit_reason="force_close"
                            )
                            t["_closed"] = True
                        qty = t.get("qty", 1)
                        self.risk.register_close(qty, symbol=t.get("symbol", ""))
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

                # Market data staleness check — restart stream if no data for 2+ minutes
                if not self.dry_run and self.md_stream:
                    is_stale = getattr(self.md_stream, "data_stale", False)
                    fell_back = getattr(self.md_stream, "fell_back", None)
                    if is_stale or (fell_back and fell_back.is_set()):
                        reason = "fell back to REST" if (fell_back and fell_back.is_set()) else "stale data"
                        logger.warning(
                            "Market data stream unhealthy (%s). Re-authenticating and restarting...", reason
                        )
                        try:
                            self.md_stream.stop()
                        except Exception:
                            pass
                        # Force token refresh before restarting stream
                        try:
                            self.api.ensure_token_valid()
                            if not self.api.md_access_token:
                                logger.warning("No md_access_token after refresh, forcing full re-auth...")
                                self.api._re_authenticate()
                        except Exception as e:
                            logger.warning("Token refresh failed during stream restart: %s", e)
                        self.md_stream = self._start_market_data()
                        if self.md_stream:
                            self._subscribe_market_data()

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
                # Market data diagnostics
                md_info = ""
                if self.md_stream:
                    md_type = type(self.md_stream).__name__
                    md_quotes = getattr(self.md_stream, "_quotes_received", "?")
                    md_dispatched = getattr(self.md_stream, "_quotes_dispatched", "?")
                    md_stale = getattr(self.md_stream, "data_stale", False)
                    md_connected = getattr(self.md_stream, "_connected", None)
                    ws_ok = md_connected.is_set() if md_connected else "?"
                    md_info = f" | md={md_type} recv={md_quotes} disp={md_dispatched} stale={md_stale} ws={ws_ok}"

                logger.info(
                    "Status | balance=%.2f | day_pnl=%.2f | to_floor=%.2f | contracts=%d/%d | trades=%d/%d | locked=%s%s%s",
                    status["balance"],
                    status["day_pnl"],
                    status["distance_to_floor"],
                    status["open_contracts"],
                    status["max_contracts"],
                    status["trades_today"],
                    status["max_daily_trades"],
                    status["locked"],
                    "" if api_ok else " | API-ERROR",
                    md_info,
                )

                # Write live status file for external monitoring
                self._write_live_status()

                # Check for external commands (from dashboard / other windows)
                cmd = read_pending_command()
                if cmd:
                    execute_command(cmd, self)

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

            # Patch missing entry fill prices (deferred fill — runs for ALL trades, not just open)
            for trade_info in self.trades_today:
                if trade_info.get("fill_price") and trade_info["fill_price"] != 0:
                    continue
                order_id = trade_info.get("order_id")
                if not order_id:
                    continue
                try:
                    detail = self.api.get_order_detail(order_id)
                    if detail and detail.get("avgPrice"):
                        avg = detail["avgPrice"]
                        trade_info["fill_price"] = avg
                        sym = trade_info.get("symbol", "")
                        journal_id = trade_info.get("journal_id", "")
                        # Patch journal — works for open trades via patch_entry_price,
                        # and for already-closed trades via direct update
                        if not trade_info.get("_closed"):
                            self.journal.patch_entry_price(sym, avg)
                        else:
                            self.journal.patch_closed_entry_price(journal_id, avg)
                        logger.info("Deferred fill patch for %s: entry_price=%.4f", sym, avg)
                except Exception as e:
                    logger.debug("Deferred fill patch failed for order %s: %s", order_id, e)

            # Close journal trades where position is now flat
            for trade_info in self.trades_today:
                journal_id = trade_info.get("journal_id")
                sym = trade_info.get("symbol")
                if not journal_id or trade_info.get("_closed"):
                    continue

                if sym not in open_base_symbols:
                    # Position is flat for this symbol — trade was closed (by SL/TP/manual)
                    exit_price, pnl, exit_reason = self._resolve_exit_fill(trade_info)
                    self.journal.record_exit_by_symbol(
                        sym, exit_price, pnl, exit_reason=exit_reason
                    )
                    self.risk.register_close(trade_info.get("qty", 1), symbol=sym)
                    trade_info["_closed"] = True
                    logger.info(
                        "Position closed for %s (flat): exit=%.4f pnl=%.2f reason=%s",
                        sym, exit_price, pnl, exit_reason,
                    )

        except Exception as e:
            logger.error("Fill sync error: %s", e)

    def _resolve_exit_fill(self, trade_info: dict) -> tuple:
        """Resolve actual exit price and P&L for a closed trade.

        Queries the SL and TP order fills from the API to determine which
        bracket leg was hit, at what price, and computes the real P&L.

        Returns:
            (exit_price, pnl, exit_reason)
        """
        sym = trade_info.get("symbol", "")
        qty = trade_info.get("qty", 1)
        direction = trade_info.get("direction", "Buy")
        entry_price = trade_info.get("fill_price", 0)
        sl_order_id = trade_info.get("sl_order_id")
        tp_order_id = trade_info.get("tp_order_id")

        spec = config.CONTRACT_SPECS.get(sym, {})
        point_value = spec.get("point_value", 1)

        exit_price = 0.0
        exit_reason = "bracket_fill"

        # Try TP order first, then SL order
        for order_id, reason in [(tp_order_id, "take_profit"), (sl_order_id, "stop_loss")]:
            if not order_id:
                continue
            try:
                detail = self.api.get_order_detail(order_id)
                if detail and detail.get("ordStatus") == "Filled":
                    avg = detail.get("avgPrice", 0)
                    if avg:
                        exit_price = avg
                        exit_reason = reason
                        break
            except Exception as e:
                logger.debug("Failed to check order %s: %s", order_id, e)

        # Fallback: query fills for the entry order to find exit fills
        if not exit_price:
            entry_order_id = trade_info.get("order_id")
            if entry_order_id:
                try:
                    fills = self.api.get_order_fills(entry_order_id)
                    # Fills for the exit side (opposite direction)
                    for fill in fills:
                        fill_action = fill.get("action", "")
                        if (direction == "Buy" and fill_action == "Sell") or \
                           (direction == "Sell" and fill_action == "Buy"):
                            exit_price = fill.get("price", 0)
                            exit_reason = "bracket_fill"
                            break
                except Exception as e:
                    logger.debug("Failed to get fills for order %s: %s", entry_order_id, e)

        # If we still don't have entry_price, try to patch it from the entry order
        if not entry_price:
            entry_order_id = trade_info.get("order_id")
            if entry_order_id:
                try:
                    detail = self.api.get_order_detail(entry_order_id)
                    if detail:
                        entry_price = detail.get("avgPrice", 0) or 0
                        if entry_price:
                            self.journal.patch_entry_price(sym, entry_price)
                            trade_info["fill_price"] = entry_price
                except Exception:
                    pass

        # Compute P&L
        pnl = 0.0
        if entry_price and exit_price:
            if direction == "Buy":
                pnl = (exit_price - entry_price) * qty * point_value
            else:
                pnl = (entry_price - exit_price) * qty * point_value
            pnl = round(pnl, 2)

        if not exit_price:
            logger.warning(
                "Could not resolve exit fill for %s (sl_order=%s tp_order=%s). "
                "Recording with price=0, pnl=0.",
                sym, sl_order_id, tp_order_id,
            )

        return exit_price, pnl, exit_reason

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
                "unrealized_pnl": status["unrealized_pnl"],
                "peak_balance": status["peak_balance"],
                "drawdown_floor": status["drawdown_floor"],
                "distance_to_floor": status["distance_to_floor"],
                "daily_profit_cap": status["daily_profit_cap"],
                "daily_profit_remaining": status["daily_profit_remaining"],
                "open_contracts": status["open_contracts"],
                "trades_today": status["trades_today"],
                "locked": status["locked"],
                "lock_reason": status["lock_reason"],
                "balance_initialized": status["balance_initialized"],
                "environment": config.ENVIRONMENT,
                "dry_run": self.dry_run,
                "active_symbols": list(self.contract_map.keys()),
                "websocket_connected": (
                    self.md_stream._connected.is_set()
                    if self.md_stream and hasattr(self.md_stream, "_connected")
                    else False
                ),
                "market_data_source": (
                    "websocket" if isinstance(self.md_stream, MarketDataStream)
                    else "rest" if self.md_stream else "none"
                ),
                "signals_today": getattr(self, "_signals_log", [])[-20:],
                "trades_log": self.trades_today[-20:],
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
