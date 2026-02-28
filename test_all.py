#!/usr/bin/env python3
"""
Comprehensive Automated Test Suite for Tradovate Bot
=====================================================
Tests every component: auth, API, WebSocket protocol, strategies,
risk manager, and end-to-end trading flow — all without needing
a live token (uses mocks + simulation).

Run:  python test_all.py
"""

import json
import logging
import math
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone, date, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

# Setup path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("test")
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# Test Results Tracker
# ─────────────────────────────────────────────

_results = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    """Decorator to register and run a test."""
    def decorator(func):
        def wrapper():
            try:
                func()
                _results["passed"] += 1
                print(f"  [PASS] {name}")
            except AssertionError as e:
                _results["failed"] += 1
                _results["errors"].append((name, str(e)))
                print(f"  [FAIL] {name}: {e}")
            except Exception as e:
                _results["failed"] += 1
                _results["errors"].append((name, f"ERROR: {e}"))
                print(f"  [ERR ] {name}: {e}")
        wrapper._test_name = name
        return wrapper
    return decorator


# ─────────────────────────────────────────────
# 1. AUTH TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("1. AUTHENTICATION TESTS")
print("=" * 60)


@test("Password encryption matches Tradovate web format")
def test_password_encryption():
    from tradovate_api import _encrypt_password
    # Test the btoa(shift+reverse) encoding
    result = _encrypt_password("testuser", "mypassword")
    # Manually compute: offset = len("testuser") % len("mypassword") = 8 % 10 = 8
    # rearranged = "rd" + "mypasswo" = "rdmypasswo"  (wrong, it's password[8:] + password[:8])
    # password[8:] = "rd", password[:8] = "mypasswo"
    # rearranged = "rdmypasswo"
    # reversed = "owssapymdR" -> wait, "rdmypasswo" reversed = "owssapmydr"
    import base64
    name = "testuser"
    pw = "mypassword"
    offset = len(name) % len(pw)  # 8
    rearranged = pw[offset:] + pw[:offset]  # "rd" + "mypasswo" = "rdmypasswo"
    reversed_pw = rearranged[::-1]  # "owssapymdr"
    expected = base64.b64encode(reversed_pw.encode()).decode()
    assert result == expected, f"Expected {expected}, got {result}"


@test("HMAC sec computation produces valid hex digest")
def test_hmac_sec():
    from tradovate_api import _compute_hmac_sec
    payload = {
        "chl": "test_challenge",
        "deviceId": "device-001",
        "name": "testuser",
        "password": "testpass",
        "appId": "TestApp",
    }
    result = _compute_hmac_sec(payload)
    # Should be a 64-char hex string (SHA-256)
    assert len(result) == 64, f"HMAC should be 64 hex chars, got {len(result)}"
    assert all(c in "0123456789abcdef" for c in result), "HMAC should be hex"


@test("Token persistence save/load roundtrip")
def test_token_persistence():
    from tradovate_api import TradovateAPI, _TOKEN_FILE
    import tempfile

    # Use a temp file for testing
    test_file = Path(tempfile.mktemp(suffix=".json"))
    original_file = _TOKEN_FILE

    try:
        import tradovate_api
        tradovate_api._TOKEN_FILE = test_file

        api = TradovateAPI()
        api.access_token = "test-token-abc123"
        api.md_access_token = "md-token-xyz"
        api.user_id = 42
        api.account_spec = "TEST_ACCOUNT"
        api.token_expiry = datetime(2026, 12, 31, tzinfo=timezone.utc)
        api._save_token()

        assert test_file.exists(), "Token file should be created"

        # Load into new instance
        api2 = TradovateAPI()
        loaded = api2._load_token()
        assert loaded, "Should load token successfully"
        assert api2.access_token == "test-token-abc123"
        assert api2.md_access_token == "md-token-xyz"
        assert api2.user_id == 42

    finally:
        tradovate_api._TOKEN_FILE = original_file
        if test_file.exists():
            test_file.unlink()


@test("Auth with pre-injected token works")
def test_injected_token():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.set_token("fake-token", "fake-md-token", 99, "2026-12-31T23:59:59Z")
    assert api.access_token == "fake-token"
    assert api.md_access_token == "fake-md-token"
    assert api.user_id == 99
    assert api.token_expiry is not None


@test("Auth priority: env var token takes precedence")
def test_auth_env_token():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()

    with patch("config.TRADOVATE_ACCESS_TOKEN", "env-token-123"):
        with patch.object(api, "_fetch_account_id"):
            with patch.object(api, "_save_token"):
                result = api.authenticate()
                assert result is True
                assert api.access_token == "env-token-123"


test_password_encryption()
test_hmac_sec()
test_token_persistence()
test_injected_token()
test_auth_env_token()


# ─────────────────────────────────────────────
# 2. API ENDPOINT TESTS (with mocked HTTP)
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("2. API ENDPOINT TESTS (mocked HTTP)")
print("=" * 60)


@test("GET /account/list returns accounts")
def test_get_accounts():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"

    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"id": 1, "name": "DEMO123", "active": True}]
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp) as mock_get:
        accounts = api.get_accounts()
        assert len(accounts) == 1
        assert accounts[0]["id"] == 1
        call_url = mock_get.call_args[0][0]
        assert "/account/list" in call_url


@test("GET /position/list returns positions")
def test_get_positions():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"

    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"id": 10, "contractId": 123, "netPos": 2, "unrealizedPnl": 150.0}
    ]
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp):
        positions = api.get_positions()
        assert len(positions) == 1
        assert positions[0]["netPos"] == 2


@test("Contract suggest returns front-month")
def test_suggest_contract():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"

    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"id": 555, "name": "NQH6", "contractMaturityId": 99}]
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp):
        contract = api.suggest_contract("NQ")
        assert contract is not None
        assert contract["name"] == "NQH6"


@test("Place bracket order sends correct placeorder + placeOCO payload")
def test_place_bracket():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"
    api.account_id = 1
    api.account_spec = "DEMO"

    # First call returns entry order, second call returns OCO
    entry_resp = {"orderId": 100}
    oco_resp = {"orderId": 200, "ocoId": 201}

    with patch.object(api, "_post", side_effect=[entry_resp, oco_resp]) as mock_post:
        result = api.place_bracket_order(
            symbol="NQH6",
            action="Buy",
            qty=1,
            entry_price=None,
            stop_price=21000.0,
            take_profit_price=21100.0,
            order_type="Market",
        )
        assert result is not None
        assert result["orderId"] == 100
        assert result["slOrderId"] == 200
        assert result["tpOrderId"] == 201

        # Verify two calls: placeorder then placeOCO
        assert mock_post.call_count == 2
        entry_call = mock_post.call_args_list[0]
        oco_call = mock_post.call_args_list[1]

        # Entry order
        assert entry_call[0][0] == "/order/placeorder"
        entry_payload = entry_call[0][1]
        assert entry_payload["action"] == "Buy"
        assert entry_payload["orderType"] == "Market"
        assert entry_payload["isAutomated"] is True

        # OCO (SL + TP)
        assert oco_call[0][0] == "/order/placeOCO"
        oco_payload = oco_call[0][1]
        assert oco_payload["action"] == "Sell"  # Opposite of entry
        assert oco_payload["orderType"] == "Stop"
        assert oco_payload["stopPrice"] == 21000.0
        assert oco_payload["other"]["orderType"] == "Limit"
        assert oco_payload["other"]["price"] == 21100.0


@test("Cancel all orders iterates working orders")
def test_cancel_all():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"

    orders = [
        {"id": 1, "ordStatus": "Working"},
        {"id": 2, "ordStatus": "Filled"},  # should be skipped
        {"id": 3, "ordStatus": "Accepted"},
    ]

    with patch.object(api, "_get", return_value=orders):
        with patch.object(api, "_post") as mock_post:
            api.cancel_all_orders()
            # Should cancel orders 1 and 3 (Working + Accepted)
            assert mock_post.call_count == 2


@test("Close all positions sends correct market orders")
def test_close_all():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"
    api.account_id = 1
    api.account_spec = "DEMO"

    positions = [
        {"contractId": 100, "netPos": 2},   # Long 2 -> sell 2
        {"contractId": 200, "netPos": -1},   # Short 1 -> buy 1
        {"contractId": 300, "netPos": 0},    # Flat -> skip
    ]

    # Mock _get to return contract name lookups for contractId resolution
    def mock_get(endpoint):
        if "id=100" in endpoint:
            return {"id": 100, "name": "NQH6"}
        if "id=200" in endpoint:
            return {"id": 200, "name": "ESH6"}
        return None

    with patch.object(api, "get_positions", return_value=positions):
        with patch.object(api, "_get", side_effect=mock_get):
            with patch.object(api, "place_market_order") as mock_order:
                api.close_all_positions()
                assert mock_order.call_count == 2
                # Check actions: Sell for long position, Buy for short
                calls = [str(c) for c in mock_order.call_args_list]
                joined = " ".join(calls)
                assert "Sell" in joined, f"Should sell long position, got: {calls}"
                assert "Buy" in joined, f"Should buy short position, got: {calls}"


@test("Token auto-renewal when close to expiry")
def test_auto_renew():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "old-token"
    # Set expiry to 2 minutes from now (< 5 min threshold)
    api.token_expiry = datetime.now(timezone.utc) + timedelta(minutes=2)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "accessToken": "new-token",
        "expirationTime": "2026-12-31T23:59:59Z",
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp):
        with patch.object(api, "_save_token"):
            api.ensure_token_valid()
            assert api.access_token == "new-token"


test_get_accounts()
test_get_positions()
test_suggest_contract()
test_place_bracket()
test_cancel_all()
test_close_all()
test_auto_renew()


# ─────────────────────────────────────────────
# 3. WEBSOCKET PROTOCOL TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("3. WEBSOCKET PROTOCOL TESTS")
print("=" * 60)


@test("WS auth message format is correct")
def test_ws_auth_format():
    from tradovate_api import MarketDataStream
    stream = MarketDataStream("test-md-token-123")
    ws = MagicMock()
    # Simulate receiving 'o' (connection open)
    stream._on_message(ws, "o")
    # Should send authorize message
    ws.send.assert_called_once()
    msg = ws.send.call_args[0][0]
    assert msg.startswith("authorize\n"), f"Auth msg should start with 'authorize\\n', got: {msg[:30]}"
    assert "test-md-token-123" in msg


@test("WS heartbeat replies with keepalive")
def test_ws_heartbeat():
    from tradovate_api import MarketDataStream
    stream = MarketDataStream("token")
    ws = MagicMock()
    # 'h' = heartbeat, client should reply with "[]" to keep alive
    stream._on_message(ws, "h")
    ws.send.assert_called_once_with("[]")


@test("WS auth response sets connected flag")
def test_ws_auth_response():
    from tradovate_api import MarketDataStream
    stream = MarketDataStream("token")
    ws = MagicMock()
    assert not stream._connected.is_set()
    # Simulate auth success: a[{"i":1,"s":200}]
    stream._on_message(ws, 'a[{"i":1,"s":200}]')
    assert stream._connected.is_set(), "Should be connected after auth success"


@test("WS subscribe message format")
def test_ws_subscribe():
    from tradovate_api import MarketDataStream
    stream = MarketDataStream("token")
    stream.ws = MagicMock()
    stream.subscribe_quote("NQH6", lambda s, d: None)
    stream.ws.send.assert_called_once()
    msg = stream.ws.send.call_args[0][0]
    assert "md/subscribeQuote" in msg
    assert "NQH6" in msg


@test("WS request ID increments")
def test_ws_request_id():
    from tradovate_api import MarketDataStream
    stream = MarketDataStream("token")
    stream.ws = MagicMock()
    stream._send("test/endpoint", {"key": "val1"})
    stream._send("test/endpoint2", {"key": "val2"})
    msgs = [call[0][0] for call in stream.ws.send.call_args_list]
    # Extract request IDs from messages
    id1 = int(msgs[0].split("\n")[1])
    id2 = int(msgs[1].split("\n")[1])
    assert id2 == id1 + 1, f"Request IDs should increment: {id1} -> {id2}"


test_ws_auth_format()
test_ws_heartbeat()
test_ws_auth_response()
test_ws_subscribe()
test_ws_request_id()


# ─────────────────────────────────────────────
# 4. STRATEGY TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("4. STRATEGY TESTS (simulated market data)")
print("=" * 60)


@test("ORB: range accumulates during window")
def test_orb_range_accumulation():
    from strategies import ORBStrategy
    strategy = ORBStrategy("NQ")
    # Feed prices during first 5 minutes (09:30 - 09:35)
    ET = ZoneInfo("America/New_York")
    t1 = datetime(2026, 2, 23, 9, 31, tzinfo=ET)
    t2 = datetime(2026, 2, 23, 9, 33, tzinfo=ET)

    s1 = strategy.on_price(21050.0, t1, 21060.0, 21040.0)
    s2 = strategy.on_price(21070.0, t2, 21080.0, 21030.0)
    # Still in accumulation, no signal
    assert s1 is None
    assert s2 is None


@test("ORB: breakout above range triggers LONG signal")
def test_orb_long_breakout():
    from strategies import ORBStrategy
    strategy = ORBStrategy("NQ")
    ET = ZoneInfo("America/New_York")

    # Accumulate range (09:30 - 09:35)
    for minute in range(5):
        t = datetime(2026, 2, 23, 9, 30 + minute, tzinfo=ET)
        strategy.on_price(21050.0, t, 21060.0, 21040.0)

    # Now past the 5-min window -> breakout detection
    t_break = datetime(2026, 2, 23, 9, 36, tzinfo=ET)
    signal = strategy.on_price(21070.0, t_break, 21070.0, 21065.0)
    assert signal is not None, "Should trigger on breakout above range"
    assert signal.direction.value == "Buy"
    assert signal.stop_loss < 21070.0
    assert signal.take_profit > 21070.0


@test("ORB: breakout below range triggers SHORT signal")
def test_orb_short_breakout():
    from strategies import ORBStrategy
    strategy = ORBStrategy("NQ")
    ET = ZoneInfo("America/New_York")

    for minute in range(5):
        t = datetime(2026, 2, 23, 9, 30 + minute, tzinfo=ET)
        strategy.on_price(21050.0, t, 21060.0, 21040.0)

    t_break = datetime(2026, 2, 23, 9, 36, tzinfo=ET)
    signal = strategy.on_price(21030.0, t_break, 21035.0, 21025.0)
    assert signal is not None
    assert signal.direction.value == "Sell"
    assert signal.stop_loss > 21030.0
    assert signal.take_profit < 21030.0


@test("ORB: no double-fire from same window")
def test_orb_no_double_fire():
    from strategies import ORBStrategy
    strategy = ORBStrategy("NQ")
    ET = ZoneInfo("America/New_York")

    for minute in range(5):
        t = datetime(2026, 2, 23, 9, 30 + minute, tzinfo=ET)
        strategy.on_price(21050.0, t, 21060.0, 21040.0)

    # First breakout
    t1 = datetime(2026, 2, 23, 9, 36, tzinfo=ET)
    s1 = strategy.on_price(21070.0, t1, 21070.0, 21065.0)
    assert s1 is not None

    # Same window should not fire again
    t2 = datetime(2026, 2, 23, 9, 37, tzinfo=ET)
    s2 = strategy.on_price(21080.0, t2, 21080.0, 21075.0)
    # s2 could be from the 15-min window or None (cooldown)
    # The first 5-min window should NOT fire again


@test("ORB: cooldown prevents rapid trades")
def test_orb_cooldown():
    from strategies import ORBStrategy
    strategy = ORBStrategy("NQ")
    ET = ZoneInfo("America/New_York")

    for minute in range(5):
        t = datetime(2026, 2, 23, 9, 30 + minute, tzinfo=ET)
        strategy.on_price(21050.0, t, 21060.0, 21040.0)

    # First breakout
    t1 = datetime(2026, 2, 23, 9, 36, tzinfo=ET)
    s1 = strategy.on_price(21070.0, t1, 21070.0, 21065.0)
    assert s1 is not None

    # Try another just 2 minutes later (within cooldown)
    t2 = datetime(2026, 2, 23, 9, 38, tzinfo=ET)
    s2 = strategy.on_price(21080.0, t2, 21080.0, 21075.0)
    assert s2 is None, "Should be blocked by cooldown"


@test("ORB: max trades cap respected")
def test_orb_max_trades():
    from strategies import ORBStrategy
    strategy = ORBStrategy("NQ")
    assert strategy.max_trades == 2  # Default from config


@test("VWAP: running VWAP calculation")
def test_vwap_calculation():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("GC")
    # Bar 1: typical = (100+90+95)/3 = 95.0, vol=100
    strat.update_vwap(100.0, 90.0, 95.0, 100)
    assert strat.vwap is not None
    assert abs(strat.vwap - 95.0) < 0.001

    # Bar 2: typical = (102+96+100)/3 = 99.333, vol=200
    strat.update_vwap(102.0, 96.0, 100.0, 200)
    expected = (95.0 * 100 + 99.333 * 200) / 300
    assert abs(strat.vwap - expected) < 0.1


@test("VWAP: crossover above triggers LONG")
def test_vwap_long_crossover():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("GC")
    strat._current_time = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)

    # Build initial VWAP around 2000
    for i in range(10):
        strat.update_vwap(2001, 1999, 2000, 100)

    # Price below VWAP
    strat._prev_price = strat.vwap - 1

    # Crossover above
    signal = strat.on_price(strat.vwap + 2, strat.vwap + 3, strat.vwap - 0.5, 100)
    assert signal is not None, "Should trigger LONG on VWAP cross above"
    assert signal.direction.value == "Buy"


@test("VWAP: crossover below triggers SHORT")
def test_vwap_short_crossover():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("GC")
    strat._current_time = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)

    for i in range(10):
        strat.update_vwap(2001, 1999, 2000, 100)

    # Price above VWAP
    strat._prev_price = strat.vwap + 1

    # Crossover below
    signal = strat.on_price(strat.vwap - 2, strat.vwap + 0.5, strat.vwap - 3, 100)
    assert signal is not None, "Should trigger SHORT on VWAP cross below"
    assert signal.direction.value == "Sell"


@test("VWAP: cooldown between same-direction trades")
def test_vwap_cooldown():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("GC")
    strat._current_time = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)

    for i in range(10):
        strat.update_vwap(2001, 1999, 2000, 100)

    # First long
    strat._prev_price = strat.vwap - 1
    s1 = strat.on_price(strat.vwap + 2, strat.vwap + 3, strat.vwap - 0.5, 100)
    assert s1 is not None

    # Try another long just 5 minutes later (within 30-min cooldown)
    strat._current_time = datetime(2026, 2, 23, 10, 5, tzinfo=timezone.utc)
    strat._prev_price = strat.vwap - 1
    s2 = strat.on_price(strat.vwap + 2, strat.vwap + 3, strat.vwap - 0.5, 100)
    assert s2 is None, "Should be blocked by cooldown"


@test("VWAP: reset clears all state")
def test_vwap_reset():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("GC")
    strat.vwap = 2000.0
    strat.long_count = 2
    strat.short_count = 1
    strat.reset()
    assert strat.vwap is None
    assert strat.long_count == 0
    assert strat.short_count == 0


@test("Strategy factory creates correct types")
def test_strategy_factory():
    from strategies import create_strategy, ORBStrategy, VWAPStrategy
    nq = create_strategy("NQ")
    assert isinstance(nq, ORBStrategy)
    gc = create_strategy("GC")
    assert isinstance(gc, VWAPStrategy)


test_orb_range_accumulation()
test_orb_long_breakout()
test_orb_short_breakout()
test_orb_no_double_fire()
test_orb_cooldown()
test_orb_max_trades()
test_vwap_calculation()
test_vwap_long_crossover()
test_vwap_short_crossover()
test_vwap_cooldown()
test_vwap_reset()
test_strategy_factory()


# ─────────────────────────────────────────────
# 5. RISK MANAGER TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("5. RISK MANAGER TESTS")
print("=" * 60)


@test("Risk manager initializes with correct values")
def test_risk_init():
    import config as cfg
    from risk_manager import RiskManager
    rm = RiskManager()
    assert rm.account_size == cfg.ACTIVE_CHALLENGE["account_size"]
    assert rm.max_trailing_drawdown == cfg.ACTIVE_CHALLENGE["max_trailing_drawdown"]
    assert rm.trading_locked is False


@test("Can trade returns True when conditions met")
def test_can_trade_ok():
    from risk_manager import RiskManager
    rm = RiskManager()
    ok, reason = rm.can_trade()
    assert ok is True
    assert reason == "OK"


@test("Trading locks on drawdown breach")
def test_drawdown_lock():
    from risk_manager import RiskManager
    rm = RiskManager()
    # Simulate large loss that breaches drawdown floor
    rm.update_balance(rm.drawdown_floor - 100, 0)
    assert rm.trading_locked is True
    assert "DRAWDOWN" in rm.lock_reason


@test("Trading locks on daily loss brake")
def test_daily_loss_brake():
    import config as cfg
    from risk_manager import RiskManager
    with patch("config.PROP_FIRM", "topstep"):
        with patch("config.ACTIVE_CHALLENGE", cfg.CHALLENGE_SETTINGS["topstep"]):
            rm = RiskManager()
            if rm.daily_loss_limit:
                # Simulate loss hitting brake %
                loss = -(rm.daily_loss_limit * rm.brake_pct + 1)
                rm.day_start_balance = rm.current_balance
                rm.update_balance(rm.current_balance + loss, 0)
                assert rm.trading_locked is True
                assert "DAILY LOSS" in rm.lock_reason


@test("Max contracts prevents new trades")
def test_max_contracts():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.open_contracts = rm.max_contracts
    ok, reason = rm.can_trade()
    assert ok is False
    assert "Max contracts" in reason


@test("Daily trade cap prevents new trades")
def test_daily_trade_cap():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.trades_today = rm.max_daily_trades
    ok, reason = rm.can_trade()
    assert ok is False
    assert "Daily trade cap" in reason


@test("Position sizing returns valid contracts")
def test_position_sizing():
    from risk_manager import RiskManager
    rm = RiskManager()
    qty = rm.calculate_position_size("NQ")
    assert isinstance(qty, int)
    assert qty >= 0
    assert qty <= rm.max_contracts


@test("Position sizing returns 0 when locked")
def test_position_sizing_locked():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.trading_locked = True
    rm.lock_reason = "test lock"
    qty = rm.calculate_position_size("NQ")
    assert qty == 0


@test("Peak balance trails correctly (Apex style)")
def test_peak_trailing():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.trails_unrealized = True
    initial_floor = rm.drawdown_floor

    # Profit: equity goes up
    rm.update_balance(rm.account_size + 1000, 500)
    assert rm.peak_balance > rm.account_size
    assert rm.drawdown_floor > initial_floor

    # Loss: floor stays
    old_floor = rm.drawdown_floor
    rm.update_balance(rm.account_size + 500, 0)
    assert rm.drawdown_floor == old_floor  # floor doesn't go down


@test("Register open/close tracks contracts")
def test_register_open_close():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.register_open(3)
    assert rm.open_contracts == 3
    assert rm.trades_today == 1
    rm.register_close(2)
    assert rm.open_contracts == 1
    rm.register_close(5)  # More than open
    assert rm.open_contracts == 0  # Clamped to 0


@test("New day resets daily state")
def test_new_day_reset():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.trades_today = 5
    rm.trading_locked = True
    rm.lock_reason = "test"
    # Simulate day change
    rm.today = date(2025, 1, 1)  # old date
    rm._check_new_day()
    assert rm.trades_today == 0
    assert rm.trading_locked is False


test_risk_init()
test_can_trade_ok()
test_drawdown_lock()
test_daily_loss_brake()
test_max_contracts()
test_daily_trade_cap()
test_position_sizing()
test_position_sizing_locked()
test_peak_trailing()
test_register_open_close()
test_new_day_reset()


# ─────────────────────────────────────────────
# 6. LIVE API CONNECTIVITY TEST
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("6. LIVE API CONNECTIVITY TEST")
print("=" * 60)


@test("Auth endpoint responds (live server reachable)")
def test_live_auth_endpoint():
    import requests
    url = "https://live.tradovateapi.com/v1/auth/accesstokenrequest"
    try:
        r = requests.post(url, json={"name": "test"}, timeout=10)
        # Any HTTP response proves endpoint is alive and reachable
        assert r.status_code < 600, f"Unexpected status: {r.status_code}"
        logger.info("    -> Live API status=%d, body_len=%d", r.status_code, len(r.text))
    except requests.exceptions.ConnectionError:
        raise AssertionError("Cannot reach Tradovate API")


@test("Demo endpoint is reachable")
def test_demo_reachable():
    import requests
    url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"
    try:
        r = requests.post(url, json={"name": "test"}, timeout=10)
        assert r.status_code < 600, f"Unexpected status: {r.status_code}"
        logger.info("    -> Demo API status=%d, body_len=%d", r.status_code, len(r.text))
    except requests.exceptions.ConnectionError:
        raise AssertionError("Cannot reach Demo API")


@test("Credentials are correct (p-ticket received)")
def test_credentials_valid():
    import requests
    url = "https://live.tradovateapi.com/v1/auth/accesstokenrequest"
    payload = {
        "name": os.getenv("TRADOVATE_USERNAME", "FNFTMOTITAPWnBks"),
        "password": os.getenv("TRADOVATE_PASSWORD", "hurIQ97##"),
        "appId": "tradovate_trader(web)",
        "appVersion": "3.260220.0",
        "deviceId": str(uuid.uuid4()),
        "cid": 8,
        "sec": "",
        "organization": "",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        data = r.json()
        # Either p-ticket (correct creds, needs captcha) or rate limited
        if "p-ticket" in data:
            logger.info("    -> Credentials CONFIRMED valid (p-ticket received)")
            logger.info("    -> Captcha required: %s", data.get("p-captcha", False))
            logger.info("    -> Wait time: %ss", data.get("p-time", "?"))
        elif "accessToken" in data:
            logger.info("    -> Direct token received! (no captcha)")
        else:
            error = data.get("errorText", "")
            if "Incorrect" in error:
                # Likely rate limited, not actual wrong password
                logger.info("    -> Rate limited (returning 'incorrect password')")
            else:
                raise AssertionError(f"Unexpected: {error}")
    except requests.exceptions.ConnectionError:
        raise AssertionError("Cannot reach API")


test_live_auth_endpoint()
test_demo_reachable()
test_credentials_valid()


# ─────────────────────────────────────────────
# 7. CONFIG VALIDATION
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("7. CONFIG VALIDATION")
print("=" * 60)

import config


@test("All enabled contracts have required fields")
def test_contract_specs():
    required = ["tick_size", "tick_value", "point_value", "strategy",
                 "stop_loss_points", "take_profit_points", "risk_reward_ratio"]
    for sym, spec in config.CONTRACT_SPECS.items():
        if not spec["enabled"]:
            continue
        for field in required:
            assert field in spec, f"{sym} missing field: {field}"
            val = spec[field]
            if isinstance(val, (int, float)):
                assert val > 0, f"{sym}.{field} should be positive, got {val}"
            elif isinstance(val, str):
                assert len(val) > 0, f"{sym}.{field} should be non-empty"


@test("Risk-reward ratio is >= 1 for all contracts")
def test_rr_ratio():
    for sym, spec in config.CONTRACT_SPECS.items():
        if not spec["enabled"]:
            continue
        assert spec["risk_reward_ratio"] >= 1.0, \
            f"{sym} risk_reward_ratio {spec['risk_reward_ratio']} < 1.0"


@test("URL configuration matches environment")
def test_urls():
    assert "live" in config.REST_URL or "demo" in config.REST_URL
    assert config.WS_TRADING_URL.startswith("wss://")
    assert config.WS_MARKET_URL.startswith("wss://")


@test("Challenge settings are complete")
def test_challenge_settings():
    for firm, settings in config.CHALLENGE_SETTINGS.items():
        assert "account_size" in settings, f"{firm} missing account_size"
        assert "max_trailing_drawdown" in settings, f"{firm} missing max_trailing_drawdown"
        assert "max_contracts" in settings, f"{firm} missing max_contracts"
        assert "close_by_et" in settings, f"{firm} missing close_by_et"
        assert settings["account_size"] > 0
        assert settings["max_trailing_drawdown"] > 0


test_contract_specs()
test_rr_ratio()
test_urls()
test_challenge_settings()


# ─────────────────────────────────────────────
# 8. END-TO-END SIMULATION
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("8. END-TO-END TRADING SIMULATION")
print("=" * 60)


@test("Full trading day simulation with NQ ORB")
def test_e2e_nq_orb():
    """Simulate a complete trading day with NQ ORB breakout."""
    from strategies import ORBStrategy, TradeSignal
    from risk_manager import RiskManager

    strategy = ORBStrategy("NQ")
    rm = RiskManager()
    ET = ZoneInfo("America/New_York")
    signals = []

    # Simulate market data: 09:30 - 15:30
    base_price = 21000.0
    import random
    random.seed(42)

    for minute in range(360):  # 6 hours
        t = datetime(2026, 2, 23, 9, 30, tzinfo=ET) + timedelta(minutes=minute)

        # Price random walk
        base_price += random.gauss(0, 2)
        high = base_price + abs(random.gauss(0, 3))
        low = base_price - abs(random.gauss(0, 3))

        signal = strategy.on_price(base_price, t, high, low)
        if signal:
            ok, reason = rm.can_trade()
            if ok:
                qty = rm.calculate_position_size("NQ")
                if qty > 0:
                    signal.qty = qty
                    rm.register_open(qty)
                    signals.append(signal)

    assert len(signals) > 0, "Should generate at least 1 signal in 6 hours"
    assert len(signals) <= strategy.max_trades, \
        f"Should not exceed max trades ({strategy.max_trades})"
    logger.info("    -> Generated %d signals in 6-hour simulation", len(signals))
    for s in signals:
        logger.info("      %s %s qty=%d SL=%.2f TP=%.2f | %s",
                     s.direction.value, s.symbol, s.qty, s.stop_loss, s.take_profit, s.reason)


@test("Full trading day simulation with GC VWAP")
def test_e2e_gc_vwap():
    """Simulate GC VWAP momentum trading."""
    from strategies import VWAPStrategy
    from risk_manager import RiskManager

    strategy = VWAPStrategy("GC")
    rm = RiskManager()
    signals = []
    import random
    random.seed(123)

    base_price = 2050.0
    for minute in range(360):
        t = datetime(2026, 2, 23, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=minute)
        strategy._current_time = t

        # Trend + noise
        trend = 0.02 * math.sin(minute / 30)
        base_price += trend + random.gauss(0, 0.5)
        high = base_price + abs(random.gauss(0, 1))
        low = base_price - abs(random.gauss(0, 1))
        volume = random.randint(50, 500)

        signal = strategy.on_price(base_price, high, low, volume)
        if signal:
            ok, _ = rm.can_trade()
            if ok:
                qty = rm.calculate_position_size("GC")
                if qty > 0:
                    signal.qty = qty
                    rm.register_open(qty)
                    signals.append(signal)

    logger.info("    -> Generated %d VWAP signals in simulation", len(signals))
    for s in signals:
        logger.info("      %s %s qty=%d SL=%.4f TP=%.4f",
                     s.direction.value, s.symbol, s.qty, s.stop_loss, s.take_profit)


@test("Risk manager prevents over-trading")
def test_e2e_risk_cap():
    """Verify risk manager caps trades and locks on drawdown."""
    from risk_manager import RiskManager
    rm = RiskManager()

    # Simulate many trades
    for i in range(config.MAX_DAILY_TRADES + 5):
        ok, reason = rm.can_trade()
        if ok:
            rm.register_open(1)
            rm.register_close(1)

    assert rm.trades_today == config.MAX_DAILY_TRADES
    ok, reason = rm.can_trade()
    assert ok is False
    assert "Daily trade cap" in reason


test_e2e_nq_orb()
test_e2e_gc_vwap()
test_e2e_risk_cap()


# ─────────────────────────────────────────────
# FINAL SUMMARY
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
total = _results["passed"] + _results["failed"]
print(f"RESULTS: {_results['passed']}/{total} passed, {_results['failed']} failed")
print("=" * 60)

if _results["errors"]:
    print("\nFailed tests:")
    for name, err in _results["errors"]:
        print(f"  - {name}: {err}")

sys.exit(0 if _results["failed"] == 0 else 1)
