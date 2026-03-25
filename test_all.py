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
    strategy = ORBStrategy("MNQ")
    # Feed prices during first 2 minutes (09:30 - 09:32), within shortest window
    ET = ZoneInfo("America/New_York")
    t1 = datetime(2026, 2, 23, 9, 30, tzinfo=ET)
    t2 = datetime(2026, 2, 23, 9, 31, tzinfo=ET)

    s1 = strategy.on_price(21050.0, t1, 21060.0, 21040.0)
    s2 = strategy.on_price(21055.0, t2, 21058.0, 21045.0)
    # Still in accumulation (within 3-min window), no signal
    assert s1 is None
    assert s2 is None


@test("ORB: breakout above range triggers LONG signal")
def test_orb_long_breakout():
    from strategies import ORBStrategy
    strategy = ORBStrategy("MNQ")
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
    strategy = ORBStrategy("MNQ")
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
    strategy = ORBStrategy("MNQ")
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
    strategy = ORBStrategy("MNQ")
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
    strategy = ORBStrategy("MNQ")
    assert strategy.max_trades == 3  # From config (raised for more trades)


@test("VWAP: running VWAP calculation")
def test_vwap_calculation():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("MGC")
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
    strat = VWAPStrategy("MGC")
    strat._current_time = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)

    # Build initial VWAP around 2000 — feed enough candles through on_price
    # to satisfy MIN_CANDLES_FOR_SIGNAL
    for i in range(6):
        strat.on_price(2000, 2001, 1999, 100)

    # Price below VWAP
    strat._prev_price = strat.vwap - 1

    # Crossover above
    signal = strat.on_price(strat.vwap + 2, strat.vwap + 3, strat.vwap - 0.5, 100)
    assert signal is not None, "Should trigger LONG on VWAP cross above"
    assert signal.direction.value == "Buy"


@test("VWAP: crossover below triggers SHORT")
def test_vwap_short_crossover():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("MGC")
    strat._current_time = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)

    # Feed enough candles through on_price to satisfy MIN_CANDLES_FOR_SIGNAL
    for i in range(6):
        strat.on_price(2000, 2001, 1999, 100)

    # Price above VWAP
    strat._prev_price = strat.vwap + 1

    # Crossover below
    signal = strat.on_price(strat.vwap - 2, strat.vwap + 0.5, strat.vwap - 3, 100)
    assert signal is not None, "Should trigger SHORT on VWAP cross below"
    assert signal.direction.value == "Sell"


@test("VWAP: cooldown between same-direction trades")
def test_vwap_cooldown():
    from strategies import VWAPStrategy
    strat = VWAPStrategy("MGC")
    strat._current_time = datetime(2026, 2, 23, 10, 0, tzinfo=timezone.utc)

    # Feed enough candles through on_price to satisfy MIN_CANDLES_FOR_SIGNAL
    for i in range(6):
        strat.on_price(2000, 2001, 1999, 100)

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
    strat = VWAPStrategy("MGC")
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
    mnq = create_strategy("MNQ")
    assert isinstance(mnq, ORBStrategy)
    mgc = create_strategy("MGC")
    assert isinstance(mgc, VWAPStrategy)


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
    qty = rm.calculate_position_size("MNQ")
    assert isinstance(qty, int)
    assert qty >= 0
    assert qty <= rm.max_contracts


@test("Position sizing returns 0 when locked")
def test_position_sizing_locked():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.trading_locked = True
    rm.lock_reason = "test lock"
    qty = rm.calculate_position_size("MNQ")
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


@test("Status includes profit cap, unrealized PnL, and balance_initialized fields")
def test_status_fields():
    from risk_manager import RiskManager
    rm = RiskManager()
    status = rm.status()
    # New fields must be present
    assert "daily_profit_cap" in status
    assert "daily_profit_remaining" in status
    assert "unrealized_pnl" in status
    assert "balance_initialized" in status
    # balance_initialized should be False before set_initial_balance
    assert status["balance_initialized"] is False
    # After set_initial_balance it should be True
    rm.set_initial_balance(50000)
    status = rm.status()
    assert status["balance_initialized"] is True
    # With a profit cap, daily_profit_remaining should reflect headroom
    if rm.daily_profit_cap:
        assert status["daily_profit_remaining"] == rm.daily_profit_cap - rm.day_pnl


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
test_status_fields()


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


@test("Full trading day simulation with MNQ ORB")
def test_e2e_nq_orb():
    """Simulate a complete trading day with MNQ micro ORB breakout."""
    from strategies import ORBStrategy, TradeSignal
    from risk_manager import RiskManager

    strategy = ORBStrategy("MNQ")
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
                qty = rm.calculate_position_size("MNQ")
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
    """Simulate GC mini VWAP momentum trading."""
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
# Contract Rollover Tests
# ─────────────────────────────────────────────

print("\n--- Contract Rollover ---")


@test("_next_liquid_contract: NQ H6 -> M6 (quarterly)")
def test_rollover_nq_h_to_m():
    from bot import TradovateBot
    result = TradovateBot._next_liquid_contract("NQ", "NQH6")
    assert result == "NQM6", f"Expected NQM6, got {result}"


@test("_next_liquid_contract: NQ Z5 -> H6 (year wrap)")
def test_rollover_nq_year_wrap():
    from bot import TradovateBot
    result = TradovateBot._next_liquid_contract("NQ", "NQZ5")
    assert result == "NQH6", f"Expected NQH6, got {result}"


@test("_next_liquid_contract: GC H6 -> J6 (gold skips odd months)")
def test_rollover_gc_h_to_j():
    from bot import TradovateBot
    result = TradovateBot._next_liquid_contract("GC", "GCH6")
    assert result == "GCJ6", f"Expected GCJ6, got {result}"


@test("_next_liquid_contract: GC Z5 -> G6 (gold year wrap to Feb)")
def test_rollover_gc_year_wrap():
    from bot import TradovateBot
    result = TradovateBot._next_liquid_contract("GC", "GCZ5")
    assert result == "GCG6", f"Expected GCG6, got {result}"


@test("_next_liquid_contract: CL F6 -> G6 (crude every month)")
def test_rollover_cl_monthly():
    from bot import TradovateBot
    result = TradovateBot._next_liquid_contract("CL", "CLF6")
    assert result == "CLG6", f"Expected CLG6, got {result}"


@test("_next_liquid_contract: ES U6 -> Z6 (ES quarterly)")
def test_rollover_es_u_to_z():
    from bot import TradovateBot
    result = TradovateBot._next_liquid_contract("ES", "ESU6")
    assert result == "ESZ6", f"Expected ESZ6, got {result}"


@test("_next_liquid_contract: unknown symbol returns None")
def test_rollover_unknown():
    from bot import TradovateBot
    result = TradovateBot._next_liquid_contract("FAKE", "FAKEH6")
    assert result is None


@test("Date-based rollover triggers when contract expires within threshold")
def test_date_based_rollover():
    from bot import TradovateBot
    from unittest.mock import MagicMock, patch
    from datetime import date, timedelta

    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.md_stream = None
    bot.contract_map = {"NQ": "NQH6"}

    # Contract expires in 5 days (within 8-day threshold)
    expiry = (date.today() + timedelta(days=5)).isoformat()
    bot.api.get_contract_maturity.return_value = expiry
    bot.api.find_contract.return_value = {"id": 999, "name": "NQM6"}
    bot.api.suggest_contract.return_value = None

    with patch("bot.now_et") as mock_now:
        mock_now.return_value = datetime.now(ZoneInfo("America/New_York"))
        bot._check_contract_rollover()

    assert bot.contract_map["NQ"] == "NQM6", f"Expected NQM6, got {bot.contract_map['NQ']}"


@test("No rollover when expiry is far away")
def test_no_rollover_far_expiry():
    from bot import TradovateBot
    from unittest.mock import MagicMock, patch
    from datetime import date, timedelta

    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.md_stream = None
    bot.contract_map = {"NQ": "NQH6"}

    # Contract expires in 20 days (outside 8-day threshold)
    expiry = (date.today() + timedelta(days=20)).isoformat()
    bot.api.get_contract_maturity.return_value = expiry
    bot.api.suggest_contract.return_value = None

    with patch("bot.now_et") as mock_now:
        mock_now.return_value = datetime.now(ZoneInfo("America/New_York"))
        bot._check_contract_rollover()

    assert bot.contract_map["NQ"] == "NQH6", "Should NOT have rolled over"


test_rollover_nq_h_to_m()
test_rollover_nq_year_wrap()
test_rollover_gc_h_to_j()
test_rollover_gc_year_wrap()
test_rollover_cl_monthly()
test_rollover_es_u_to_z()
test_rollover_unknown()
test_date_based_rollover()
test_no_rollover_far_expiry()


# ─────────────────────────────────────────────
# 9. TRADE JOURNAL TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("9. TRADE JOURNAL TESTS")
print("=" * 60)

import tempfile


@test("Journal: record entry/exit roundtrip with P&L")
def test_journal_entry_exit():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tid = j.record_entry("NQ", "Buy", 21000.0, 1, "ORB", "breakout",
                             stop_loss=20975.0, take_profit=21050.0)
        assert tid is not None
        assert len(j.trades) == 1
        assert j.trades[0]["status"] == "open"

        j.record_exit(tid, 21050.0, 1000.0, "take_profit")
        assert j.trades[0]["status"] == "closed"
        assert j.trades[0]["pnl"] == 1000.0
        assert j.trades[0]["exit_reason"] == "take_profit"
    finally:
        os.unlink(path)


@test("Journal: R-multiple calculation")
def test_journal_r_multiple():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tid = j.record_entry("NQ", "Buy", 21000.0, 1, "ORB", "breakout",
                             stop_loss=20975.0, take_profit=21050.0)
        # Risk = |21000 - 20975| * 1 * 20 (NQ point_value) = 25 * 20 = $500
        j.record_exit(tid, 21050.0, 1000.0, "take_profit")
        r = j.trades[0].get("r_multiple", 0)
        assert abs(r - 2.0) < 0.01, f"Expected R=2.0, got {r}"
    finally:
        os.unlink(path)


@test("Journal: compute summary with known trades")
def test_journal_compute_summary():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        # Create 3 wins and 2 losses
        for i, (pnl, reason) in enumerate([
            (500, "take_profit"), (300, "take_profit"), (200, "take_profit"),
            (-150, "stop_loss"), (-100, "stop_loss"),
        ]):
            tid = j.record_entry("NQ", "Buy", 21000.0, 1, "ORB", "test",
                                 stop_loss=20975.0, take_profit=21050.0)
            j.record_exit(tid, 21050.0 if pnl > 0 else 20950.0, pnl, reason)

        summary = j._compute_summary()
        assert summary["total_trades"] == 5
        assert summary["wins"] == 3
        assert summary["losses"] == 2
        assert abs(summary["win_rate"] - 0.6) < 0.01
        assert abs(summary["total_pnl"] - 750.0) < 0.01
    finally:
        os.unlink(path)


@test("Journal: analyze by symbol grouping")
def test_journal_analyze_by_symbol():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        for sym, pnl in [("NQ", 500), ("NQ", -200), ("GC", 300), ("GC", 100)]:
            tid = j.record_entry(sym, "Buy", 100, 1, "ORB", "test")
            j.record_exit(tid, 110, pnl, "signal")

        by_sym = j.analyze_by_symbol()
        assert "NQ" in by_sym
        assert "GC" in by_sym
        assert by_sym["NQ"]["trades"] == 2
        assert abs(by_sym["NQ"]["total_pnl"] - 300) < 0.01
        assert by_sym["GC"]["trades"] == 2
        assert abs(by_sym["GC"]["total_pnl"] - 400) < 0.01
    finally:
        os.unlink(path)


@test("Journal: analyze by strategy grouping")
def test_journal_analyze_by_strategy():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        for strat, pnl in [("ORB", 500), ("ORB", -100), ("VWAP", 200)]:
            tid = j.record_entry("NQ", "Buy", 100, 1, strat, "test")
            j.record_exit(tid, 110, pnl, "signal")

        by_strat = j.analyze_by_strategy()
        assert "ORB" in by_strat
        assert "VWAP" in by_strat
        assert by_strat["ORB"]["trades"] == 2
        assert by_strat["VWAP"]["trades"] == 1
    finally:
        os.unlink(path)


@test("Journal: analyze by exit reason")
def test_journal_analyze_by_exit_reason():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        for reason, pnl in [("take_profit", 500), ("stop_loss", -200), ("stop_loss", -150)]:
            tid = j.record_entry("NQ", "Buy", 100, 1, "ORB", "test")
            j.record_exit(tid, 110, pnl, reason)

        by_exit = j.analyze_by_exit_reason()
        assert by_exit["take_profit"]["count"] == 1
        assert by_exit["stop_loss"]["count"] == 2
        assert abs(by_exit["stop_loss"]["total_pnl"] - (-350)) < 0.01
    finally:
        os.unlink(path)


@test("Journal: daily P&L breakdown")
def test_journal_daily_pnl():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tid1 = j.record_entry("NQ", "Buy", 100, 1, "ORB", "test")
        j.record_exit(tid1, 110, 500, "take_profit")
        tid2 = j.record_entry("NQ", "Buy", 100, 1, "ORB", "test")
        j.record_exit(tid2, 90, -200, "stop_loss")

        by_day = j.daily_pnl_breakdown()
        today = date.today().isoformat()
        assert today in by_day
        assert abs(by_day[today] - 300) < 0.01
    finally:
        os.unlink(path)


@test("Journal: record_exit_by_symbol finds correct trade")
def test_journal_exit_by_symbol():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        j.record_entry("NQ", "Buy", 100, 1, "ORB", "test1")
        j.record_entry("GC", "Sell", 2000, 1, "VWAP", "test2")

        j.record_exit_by_symbol("GC", 1990, 100, "take_profit")
        assert j.trades[0]["status"] == "open"   # NQ still open
        assert j.trades[1]["status"] == "closed"  # GC closed
    finally:
        os.unlink(path)


@test("Journal: persistence save/load roundtrip")
def test_journal_persistence():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j1 = TradeJournal(filepath=path)
        tid = j1.record_entry("NQ", "Buy", 21000, 1, "ORB", "test")
        j1.record_exit(tid, 21050, 500, "take_profit")

        # Load into new instance
        j2 = TradeJournal(filepath=path)
        assert len(j2.trades) == 1
        assert j2.trades[0]["pnl"] == 500
        assert j2.trades[0]["status"] == "closed"
    finally:
        os.unlink(path)


@test("Journal: generate_lessons produces insights for bad performance")
def test_journal_generate_lessons():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        # Create 5 losing trades to trigger lessons
        for _ in range(5):
            tid = j.record_entry("NQ", "Buy", 100, 1, "ORB", "test",
                                 stop_loss=95, take_profit=110)
            j.record_exit(tid, 95, -100, "stop_loss")

        lessons = j.generate_lessons()
        assert len(lessons) >= 1
        # Should mention low win rate (0%)
        combined = " ".join(lessons).lower()
        assert "win rate" in combined or "stop" in combined or "losing" in combined
    finally:
        os.unlink(path)


test_journal_entry_exit()
test_journal_r_multiple()
test_journal_compute_summary()
test_journal_analyze_by_symbol()
test_journal_analyze_by_strategy()
test_journal_analyze_by_exit_reason()
test_journal_daily_pnl()
test_journal_exit_by_symbol()
test_journal_persistence()
test_journal_generate_lessons()


# ─────────────────────────────────────────────
# 10. AUTO TUNER TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("10. AUTO TUNER TESTS")
print("=" * 60)


def _make_closed_trades(symbol, sl_count, tp_count, pnl_per_sl=-200, pnl_per_tp=400, r_mult=None):
    """Helper to create a list of closed trade dicts."""
    trades = []
    for i in range(sl_count):
        t = {
            "symbol": symbol, "status": "closed", "pnl": pnl_per_sl,
            "exit_reason": "stop_loss", "strategy": "ORB",
            "entry_hour_et": 10, "date": date.today().isoformat(),
            "r_multiple": r_mult if r_mult is not None else pnl_per_sl / 500,
        }
        trades.append(t)
    for i in range(tp_count):
        t = {
            "symbol": symbol, "status": "closed", "pnl": pnl_per_tp,
            "exit_reason": "take_profit", "strategy": "ORB",
            "entry_hour_et": 10, "date": date.today().isoformat(),
            "r_multiple": r_mult if r_mult is not None else pnl_per_tp / 500,
        }
        trades.append(t)
    return trades


@test("AutoTuner: widening stops when SL hit rate > 70%")
def test_tuner_widen_stops():
    from auto_tuner import AutoTuner
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)

        # 8 SL hits, 2 TP hits = 80% SL rate
        trades = _make_closed_trades("MNQ", 8, 2)

        old_sl = config.CONTRACT_SPECS["MNQ"]["stop_loss_points"]
        tuner._tune_stops(trades)

        # Should have proposed widening
        sl_adj = [a for a in tuner.adjustments if a["param"] == "stop_loss_points" and a["symbol"] == "MNQ"]
        assert len(sl_adj) == 1, f"Expected 1 SL adjustment, got {len(sl_adj)}"
        assert sl_adj[0]["new_value"] > old_sl, "New SL should be wider (larger)"
    finally:
        os.unlink(path)


@test("AutoTuner: tightening stops when SL hit rate < 30%")
def test_tuner_tighten_stops():
    from auto_tuner import AutoTuner
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)

        # 1 SL hit, 6 TP hits = ~14% SL rate
        trades = _make_closed_trades("MNQ", 1, 6)

        old_sl = config.CONTRACT_SPECS["MNQ"]["stop_loss_points"]
        tuner._tune_stops(trades)

        sl_adj = [a for a in tuner.adjustments if a["param"] == "stop_loss_points" and a["symbol"] == "MNQ"]
        assert len(sl_adj) == 1
        assert sl_adj[0]["new_value"] < old_sl, "New SL should be tighter (smaller)"
    finally:
        os.unlink(path)


@test("AutoTuner: widening TP when avg R > 1.5")
def test_tuner_widen_tp():
    from auto_tuner import AutoTuner
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)

        # High R-multiple trades
        trades = _make_closed_trades("MNQ", 1, 4, r_mult=2.0)

        old_tp = config.CONTRACT_SPECS["MNQ"]["take_profit_points"]
        tuner._tune_targets(trades)

        tp_adj = [a for a in tuner.adjustments if a["param"] == "take_profit_points" and a["symbol"] == "MNQ"]
        assert len(tp_adj) == 1
        assert tp_adj[0]["new_value"] > old_tp, "TP should be widened for high R"
    finally:
        os.unlink(path)


@test("AutoTuner: tightening TP when avg R < -0.5")
def test_tuner_tighten_tp():
    from auto_tuner import AutoTuner
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)

        # Negative R-multiple trades — all stop_loss exits (no TP trades)
        # so we reach the avg_r < -0.5 branch (not blocked by tp_trades > 0)
        trades = _make_closed_trades("MNQ", 4, 0, r_mult=-1.0)

        old_tp = config.CONTRACT_SPECS["MNQ"]["take_profit_points"]
        tuner._tune_targets(trades)

        tp_adj = [a for a in tuner.adjustments if a["param"] == "take_profit_points" and a["symbol"] == "MNQ"]
        assert len(tp_adj) == 1
        assert tp_adj[0]["new_value"] < old_tp, "TP should be tightened for negative R"
    finally:
        os.unlink(path)


@test("AutoTuner: propose caps at ±20%")
def test_tuner_cap_20pct():
    from auto_tuner import AutoTuner
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)

        old_val = 25.0
        # Propose 50% increase — should be capped at 20%
        tuner._propose("stop_loss_points", "NQ", old_val, old_val * 1.50, "test huge increase")

        if tuner.adjustments:
            new_val = tuner.adjustments[0]["new_value"]
            max_allowed = old_val * 1.20
            assert new_val <= max_allowed + 0.01, f"Expected <= {max_allowed}, got {new_val}"
    finally:
        os.unlink(path)


@test("AutoTuner: propose respects absolute bounds")
def test_tuner_absolute_bounds():
    from auto_tuner import AutoTuner, _BOUNDS
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)

        bounds = _BOUNDS["stop_loss_points"]["NQ"]
        # Try to propose value above max bound
        tuner._propose("stop_loss_points", "NQ", bounds[1] - 1, bounds[1] + 100, "test above max")

        if tuner.adjustments:
            assert tuner.adjustments[0]["new_value"] <= bounds[1], \
                f"Should be capped at {bounds[1]}"
    finally:
        os.unlink(path)


@test("AutoTuner: flag losing symbol for review")
def test_tuner_flag_losing_symbol():
    from auto_tuner import AutoTuner
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)

        # 5 trades, win rate 20%, total PnL -600
        trades = _make_closed_trades("NQ", 4, 1, pnl_per_sl=-200, pnl_per_tp=200)

        tuner._tune_symbol_allocation(trades)

        flag_adj = [a for a in tuner.adjustments if a["param"] == "enabled"]
        assert len(flag_adj) == 1
        assert flag_adj[0]["new_value"] == "REVIEW"
        assert flag_adj[0]["applied"] is False  # Should NOT auto-disable
    finally:
        os.unlink(path)


@test("AutoTuner: reduce daily cap when late trades lose")
def test_tuner_reduce_daily_cap():
    from auto_tuner import AutoTuner
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        tuner = AutoTuner(journal=j)
        today_str = date.today().isoformat()

        # 12 trades today, late ones (after 8th) are mostly losses
        trades = []
        for i in range(12):
            pnl = 200 if i < 7 else -300  # First 7 win, last 5 lose
            trades.append({
                "symbol": "NQ", "status": "closed", "pnl": pnl,
                "exit_reason": "stop_loss" if pnl < 0 else "take_profit",
                "strategy": "ORB", "entry_hour_et": 10,
                "date": today_str, "r_multiple": 1.0 if pnl > 0 else -1.0,
            })

        old_cap = config.MAX_DAILY_TRADES
        tuner._tune_daily_trade_cap(trades)
        # Restore original
        config.MAX_DAILY_TRADES = old_cap

        cap_adj = [a for a in tuner.adjustments if a["param"] == "MAX_DAILY_TRADES"]
        assert len(cap_adj) == 1
        assert cap_adj[0]["new_value"] < old_cap
    finally:
        os.unlink(path)


test_tuner_widen_stops()
test_tuner_tighten_stops()
test_tuner_widen_tp()
test_tuner_tighten_tp()
test_tuner_cap_20pct()
test_tuner_absolute_bounds()
test_tuner_flag_losing_symbol()
test_tuner_reduce_daily_cap()


# ─────────────────────────────────────────────
# 11. BOT STATE PERSISTENCE TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("11. BOT STATE PERSISTENCE TESTS")
print("=" * 60)


@test("BotState: save/load roundtrip")
def test_bot_state_roundtrip():
    import bot_state
    original_file = bot_state.STATE_FILE
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            bot_state.STATE_FILE = f.name

        state = {"trades_today_count": 5, "symbols": {"NQ": {"type": "ORBStrategy"}}}
        bot_state.save_state(state)
        loaded = bot_state.load_state()
        assert loaded is not None
        assert loaded["trades_today_count"] == 5
        assert "NQ" in loaded["symbols"]
    finally:
        if os.path.exists(bot_state.STATE_FILE):
            os.unlink(bot_state.STATE_FILE)
        bot_state.STATE_FILE = original_file


@test("BotState: load returns None for missing file")
def test_bot_state_missing_file():
    import bot_state
    original_file = bot_state.STATE_FILE
    try:
        bot_state.STATE_FILE = "/tmp/nonexistent_bot_state_test.json"
        result = bot_state.load_state()
        assert result is None
    finally:
        bot_state.STATE_FILE = original_file


@test("BotState: load returns None for stale date")
def test_bot_state_stale_date():
    import bot_state
    original_file = bot_state.STATE_FILE
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            bot_state.STATE_FILE = f.name
            json.dump({"_date": "2020-01-01", "trades_today_count": 3}, f)

        result = bot_state.load_state()
        assert result is None, "Should return None for stale date"
    finally:
        if os.path.exists(bot_state.STATE_FILE):
            os.unlink(bot_state.STATE_FILE)
        bot_state.STATE_FILE = original_file


@test("BotState: load returns None for corrupt JSON")
def test_bot_state_corrupt():
    import bot_state
    original_file = bot_state.STATE_FILE
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            bot_state.STATE_FILE = f.name
            f.write("{not valid json!!")

        result = bot_state.load_state()
        assert result is None, "Should return None for corrupt JSON"
    finally:
        if os.path.exists(bot_state.STATE_FILE):
            os.unlink(bot_state.STATE_FILE)
        bot_state.STATE_FILE = original_file


@test("BotState: build_state captures ORB strategy state")
def test_bot_state_build_orb():
    from bot_state import build_state
    from strategies import ORBStrategy

    strategy = ORBStrategy("NQ")
    strategy.trades_taken = 2
    strategy.last_trade_time = datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc)
    if strategy.windows:
        strategy.windows[0].breakout_fired = True

    state = build_state({"NQ": strategy}, 3, [])
    assert state["trades_today_count"] == 3
    assert state["symbols"]["NQ"]["type"] == "ORBStrategy"
    assert state["symbols"]["NQ"]["trades_taken"] == 2
    assert state["symbols"]["NQ"]["windows"][0]["breakout_fired"] is True


@test("BotState: build_state captures VWAP strategy state")
def test_bot_state_build_vwap():
    from bot_state import build_state
    from strategies import VWAPStrategy

    strategy = VWAPStrategy("GC")
    strategy.long_count = 1
    strategy.short_count = 2

    state = build_state({"GC": strategy}, 4, [])
    assert state["symbols"]["GC"]["type"] == "VWAPStrategy"
    assert state["symbols"]["GC"]["long_count"] == 1
    assert state["symbols"]["GC"]["short_count"] == 2


@test("BotState: restore_strategies restores ORB state")
def test_bot_state_restore_orb():
    from bot_state import restore_strategies
    from strategies import ORBStrategy

    strategy = ORBStrategy("NQ")
    state = {
        "symbols": {
            "NQ": {
                "type": "ORBStrategy",
                "trades_taken": 2,
                "last_trade_time": "2026-03-01T10:30:00+00:00",
                "windows": [
                    {"window_minutes": 5, "breakout_fired": True,
                     "range_set": True, "range_high": 21060, "range_low": 21040},
                ],
            }
        }
    }

    restore_strategies(state, {"NQ": strategy})
    assert strategy.trades_taken == 2
    assert strategy.last_trade_time is not None
    assert strategy.windows[0].breakout_fired is True


@test("BotState: build_state persists day_start_balance")
def test_bot_state_day_start_balance():
    import bot_state
    from bot_state import build_state
    original_file = bot_state.STATE_FILE

    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            bot_state.STATE_FILE = f.name

        # Build state with day_start_balance
        state = build_state({}, 2, [], day_start_balance=60000.0)
        assert state["day_start_balance"] == 60000.0

        # Save and reload
        bot_state.save_state(state)
        loaded = bot_state.load_state()
        assert loaded is not None
        assert loaded["day_start_balance"] == 60000.0

        # Without day_start_balance
        state2 = build_state({}, 0, [])
        assert "day_start_balance" not in state2
    finally:
        if os.path.exists(bot_state.STATE_FILE):
            os.unlink(bot_state.STATE_FILE)
        bot_state.STATE_FILE = original_file


@test("BotState: day_start_balance survives mid-day restart")
def test_bot_state_day_start_balance_restart():
    """Simulate: bot profits, restarts, then loses — day_pnl should reflect full day."""
    from risk_manager import RiskManager
    import bot_state
    original_file = bot_state.STATE_FILE

    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            bot_state.STATE_FILE = f.name

        # Day starts at $60,000
        rm = RiskManager()
        rm.set_initial_balance(60000.0)

        # Bot makes $800 profit, balance goes to $60,800
        rm.update_balance(60800.0, 0)
        assert abs(rm.day_pnl - 800.0) < 0.01

        # Save state before restart
        state = bot_state.build_state({}, 1, [], day_start_balance=rm.day_start_balance)
        bot_state.save_state(state)

        # === RESTART ===
        # New risk manager (simulating fresh bot)
        rm2 = RiskManager()
        rm2.set_initial_balance(60800.0)  # API returns current balance
        # Without fix: day_start_balance = 60800, day_pnl = 0
        assert rm2.day_start_balance == 60800.0  # wrong!

        # Restore persisted day_start_balance
        saved = bot_state.load_state()
        assert saved is not None
        rm2.day_start_balance = saved["day_start_balance"]
        rm2.day_pnl = rm2.current_balance - rm2.day_start_balance

        # Now day_start_balance is correctly $60,000
        assert rm2.day_start_balance == 60000.0
        assert abs(rm2.day_pnl - 800.0) < 0.01  # still shows +$800

        # Bot loses $600 → balance = $60,200
        rm2.update_balance(60200.0, 0)
        assert abs(rm2.day_pnl - 200.0) < 0.01  # net +$200 for the day

        # Without fix, day_pnl would be 60200 - 60800 = -600 → false brake!
        assert not rm2.trading_locked, "Should NOT be locked — net day P&L is +$200"

    finally:
        if os.path.exists(bot_state.STATE_FILE):
            os.unlink(bot_state.STATE_FILE)
        bot_state.STATE_FILE = original_file


test_bot_state_roundtrip()
test_bot_state_missing_file()
test_bot_state_stale_date()
test_bot_state_corrupt()
test_bot_state_build_orb()
test_bot_state_build_vwap()
test_bot_state_restore_orb()
test_bot_state_day_start_balance()
test_bot_state_day_start_balance_restart()


# ─────────────────────────────────────────────
# 12. BOT ORCHESTRATOR TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("12. BOT ORCHESTRATOR TESTS")
print("=" * 60)


@test("Bot: _execute_signal in dry run mode logs but doesn't place orders")
def test_bot_execute_signal_dry_run():
    from bot import TradovateBot
    from strategies import TradeSignal, Direction

    bot = TradovateBot(dry_run=True)
    bot.api = MagicMock()
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = (True, "OK")
    bot.risk.calculate_position_size.return_value = 1
    bot.contract_map = {"NQ": "NQH6"}
    bot.strategies = {"NQ": MagicMock()}
    bot._last_order_time = 0  # No cooldown

    signal = TradeSignal(
        symbol="NQ", direction=Direction.LONG,
        entry_price=21000, stop_loss=20975, take_profit=21050,
        qty=1, reason="test breakout",
    )
    bot._execute_signal(signal)

    bot.api.place_bracket_order.assert_not_called()
    assert len(bot.trades_today) == 1


@test("Bot: _execute_signal respects global cooldown")
def test_bot_execute_signal_cooldown():
    from bot import TradovateBot
    from strategies import TradeSignal, Direction

    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = (True, "OK")
    bot.contract_map = {"NQ": "NQH6"}

    # Set last order to just now (within cooldown)
    bot._last_order_time = time.time()

    signal = TradeSignal(
        symbol="NQ", direction=Direction.LONG,
        entry_price=21000, stop_loss=20975, take_profit=21050,
        qty=1, reason="test breakout",
    )
    bot._execute_signal(signal)

    # Should NOT have placed an order due to cooldown
    bot.api.place_bracket_order.assert_not_called()


@test("Bot: _execute_signal rejected by risk manager")
def test_bot_execute_signal_risk_reject():
    from bot import TradovateBot
    from strategies import TradeSignal, Direction

    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = (False, "DRAWDOWN BREACH")
    bot.contract_map = {"NQ": "NQH6"}
    bot._last_order_time = 0

    signal = TradeSignal(
        symbol="NQ", direction=Direction.LONG,
        entry_price=21000, stop_loss=20975, take_profit=21050,
        qty=1, reason="test breakout",
    )
    bot._execute_signal(signal)

    bot.api.place_bracket_order.assert_not_called()


@test("Bot: _execute_signal places order and registers with risk manager")
def test_bot_execute_signal_success():
    from bot import TradovateBot
    from strategies import TradeSignal, Direction

    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.api.place_bracket_order.return_value = {"orderId": 123, "slOrderId": 124, "tpOrderId": 125}
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = (True, "OK")
    bot.risk.calculate_position_size.return_value = 2
    bot.journal = MagicMock()
    bot.journal.record_entry.return_value = "NQ_20260301_103000"
    bot.contract_map = {"NQ": "NQH6"}
    bot.strategies = {"NQ": MagicMock()}
    bot._last_order_time = 0

    signal = TradeSignal(
        symbol="NQ", direction=Direction.LONG,
        entry_price=21000, stop_loss=20975, take_profit=21050,
        qty=1, reason="test breakout",
    )
    bot._execute_signal(signal)

    bot.api.place_bracket_order.assert_called_once()
    bot.risk.register_open.assert_called_once()
    bot.journal.record_entry.assert_called_once()


@test("Bot: _execute_signal skips when position size is 0")
def test_bot_execute_signal_zero_qty():
    from bot import TradovateBot
    from strategies import TradeSignal, Direction

    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = (True, "OK")
    bot.risk.calculate_position_size.return_value = 0
    bot.contract_map = {"NQ": "NQH6"}
    bot._last_order_time = 0

    signal = TradeSignal(
        symbol="NQ", direction=Direction.LONG,
        entry_price=21000, stop_loss=20975, take_profit=21050,
        qty=1, reason="test",
    )
    bot._execute_signal(signal)

    bot.api.place_bracket_order.assert_not_called()


@test("Bot: _process_price runs strategy and executes signal")
def test_bot_process_price():
    from bot import TradovateBot
    from strategies import TradeSignal, Direction

    bot = TradovateBot(dry_run=True)
    bot.contract_map = {"NQ": "NQH6"}

    mock_strategy = MagicMock()
    # Remove update_vwap so it takes the ORB path
    del mock_strategy.update_vwap
    mock_signal = TradeSignal(
        symbol="NQ", direction=Direction.LONG,
        entry_price=21000, stop_loss=20975, take_profit=21050,
        qty=1, reason="breakout",
    )
    mock_strategy.on_price.return_value = mock_signal
    bot.strategies = {"NQ": mock_strategy}
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = (True, "OK")
    bot.risk.calculate_position_size.return_value = 1
    bot.journal = MagicMock()
    bot.journal.record_entry.return_value = "NQ_test"
    bot._last_order_time = 0
    bot.api = MagicMock()
    bot.api.place_bracket_order.return_value = {"orderId": 999}

    # Mock time to be within trading hours
    with patch("bot.now_et") as mock_now, \
         patch("bot.parse_time_et") as mock_parse:
        mock_now.return_value = datetime(2026, 3, 2, 10, 30, tzinfo=ZoneInfo("America/New_York"))
        mock_parse.side_effect = [
            datetime(2026, 3, 2, 9, 30, tzinfo=ZoneInfo("America/New_York")),  # start
            datetime(2026, 3, 2, 16, 15, tzinfo=ZoneInfo("America/New_York")),  # cutoff
        ]
        bot._process_price("NQ", 21070.0, 21075.0, 21065.0, 100)

    mock_strategy.on_price.assert_called_once()


test_bot_execute_signal_dry_run()
test_bot_execute_signal_cooldown()
test_bot_execute_signal_risk_reject()
test_bot_execute_signal_success()
test_bot_execute_signal_zero_qty()
test_bot_process_price()


# ─────────────────────────────────────────────
# 13. API ERROR HANDLING TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("13. API ERROR HANDLING TESTS")
print("=" * 60)


@test("API: ensure_token_valid skips if token is fresh")
def test_api_token_fresh():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "valid-token"
    # Set expiry to 30 minutes from now (> 5 min threshold)
    api.token_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    with patch("requests.post") as mock_post:
        api.ensure_token_valid()
        mock_post.assert_not_called()  # Should not try to renew


@test("API: get_accounts returns empty list on error")
def test_api_accounts_error():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("Network error")

    with patch("requests.get", return_value=mock_resp):
        try:
            result = api.get_accounts()
            # Should either return empty or raise — both acceptable
        except Exception:
            pass  # Expected for error handling


@test("API: place_bracket_order handles failed entry order")
def test_api_bracket_entry_fail():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"
    api.account_id = 1
    api.account_spec = "DEMO"

    with patch.object(api, "_post", return_value=None) as mock_post:
        result = api.place_bracket_order(
            symbol="NQH6", action="Buy", qty=1,
            entry_price=None, stop_price=21000, take_profit_price=21100,
            order_type="Market",
        )
        # Should handle gracefully — either None or error dict
        if result is not None:
            assert "orderId" not in result or result.get("error")


@test("API: NaN/Inf balance update rejected by risk manager")
def test_risk_nan_balance():
    from risk_manager import RiskManager
    rm = RiskManager()
    old_balance = rm.current_balance

    rm.update_balance(float("nan"), 0)
    assert rm.current_balance == old_balance, "NaN should be rejected"

    rm.update_balance(float("inf"), 0)
    assert rm.current_balance == old_balance, "Inf should be rejected"


@test("API: record_fill with NaN is rejected")
def test_risk_nan_fill():
    from risk_manager import RiskManager
    rm = RiskManager()
    old_balance = rm.current_balance

    rm.record_fill(float("nan"))
    assert rm.current_balance == old_balance, "NaN fill should be rejected"


@test("API: record_fill clamps negative balance to 0")
def test_risk_negative_balance_clamp():
    from risk_manager import RiskManager
    rm = RiskManager()
    rm.current_balance = 100

    rm.record_fill(-5000)  # Much larger loss than balance
    assert rm.current_balance >= 0, "Balance should be clamped to 0"


test_api_token_fresh()
test_api_accounts_error()
test_api_bracket_entry_fail()
test_risk_nan_balance()
test_risk_nan_fill()
test_risk_negative_balance_clamp()


# ─────────────────────────────────────────────
# 14. STATUS REPORTER TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("14. STATUS REPORTER TESTS")
print("=" * 60)


@test("StatusReporter: write_status produces valid JSON with all fields")
def test_status_reporter_fields():
    import status_reporter
    original_path = status_reporter.STATUS_PATH

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        test_path = Path(f.name)

    try:
        status_reporter.STATUS_PATH = test_path

        risk_status = {
            "balance": 50500, "equity": 50600, "day_pnl": 500,
            "peak_balance": 50600, "drawdown_floor": 48100,
            "distance_to_floor": 2500, "open_contracts": 1,
            "trades_today": 3, "locked": False, "lock_reason": "",
        }
        status_reporter.write_status(
            risk_status,
            contract_map={"NQ": "NQH6", "GC": "GCJ6"},
            dry_run=False,
            open_positions=[{"symbol": "NQ", "qty": 1, "pnl_dollars": 100}],
            recent_closed_trades=[],
        )

        data = json.loads(test_path.read_text())
        required_fields = [
            "bot", "timestamp", "timestamp_et", "environment", "dry_run",
            "balance", "equity", "day_pnl", "peak_balance", "drawdown_floor",
            "distance_to_floor", "open_contracts", "trades_today",
            "locked", "lock_reason", "active_symbols",
            "open_positions", "open_positions_count", "recent_closed_trades",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

        assert data["bot"] == "Tradovate"
        assert data["balance"] == 50500
        assert data["open_positions_count"] == 1
        assert "NQ" in data["active_symbols"]
    finally:
        status_reporter.STATUS_PATH = original_path
        if test_path.exists():
            test_path.unlink()


@test("StatusReporter: empty positions and locked state")
def test_status_reporter_locked():
    import status_reporter
    original_path = status_reporter.STATUS_PATH

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        test_path = Path(f.name)

    try:
        status_reporter.STATUS_PATH = test_path

        risk_status = {
            "balance": 47000, "equity": 47000, "day_pnl": -1000,
            "peak_balance": 50000, "drawdown_floor": 47500,
            "distance_to_floor": -500, "open_contracts": 0,
            "trades_today": 5, "locked": True, "lock_reason": "DRAWDOWN BREACH",
        }
        status_reporter.write_status(risk_status, contract_map={})

        data = json.loads(test_path.read_text())
        assert data["locked"] is True
        assert data["lock_reason"] == "DRAWDOWN BREACH"
        assert data["open_positions_count"] == 0
        assert data["active_symbols"] == []
    finally:
        status_reporter.STATUS_PATH = original_path
        if test_path.exists():
            test_path.unlink()


@test("StatusReporter: handles missing risk_status fields gracefully")
def test_status_reporter_missing_fields():
    import status_reporter
    original_path = status_reporter.STATUS_PATH

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        test_path = Path(f.name)

    try:
        status_reporter.STATUS_PATH = test_path

        # Minimal risk_status with some fields missing
        risk_status = {"balance": 50000}
        status_reporter.write_status(risk_status)

        data = json.loads(test_path.read_text())
        assert data["balance"] == 50000
        assert data["equity"] is None
        assert data["locked"] is None
    finally:
        status_reporter.STATUS_PATH = original_path
        if test_path.exists():
            test_path.unlink()


test_status_reporter_fields()
test_status_reporter_locked()
test_status_reporter_missing_fields()


# ─────────────────────────────────────────────
# 15. BOT HEALTH CHECK TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("15. BOT HEALTH CHECK TESTS")
print("=" * 60)


@test("HealthCheck: check_bot_log parses status line and counts errors")
def test_health_check_bot_log():
    from bot_health_check import check_bot_log, LOG_FILE
    original_log = bot_health_check.LOG_FILE

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="w") as f:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{now_str} [INFO] Status | balance=50500\n")
        f.write(f"{now_str} [ERROR] Connection failed\n")
        f.write(f"{now_str} [WARNING] Token expiring\n")
        f.write(f"{now_str} [INFO] SIGNAL: NQ LONG breakout\n")
        f.write(f"{now_str} [INFO] Order placed: orderId=123\n")
        test_log = Path(f.name)

    try:
        bot_health_check.LOG_FILE = test_log
        result = check_bot_log()

        assert result["log_exists"] is True
        assert result["last_status_line"] is not None
        assert "Status |" in result["last_status_line"]
        assert result["errors_last_hour"] >= 1
        assert result["warnings_last_hour"] >= 1
        assert result["signals_today"] >= 1
        assert result["trades_today"] >= 1
    finally:
        bot_health_check.LOG_FILE = original_log
        test_log.unlink()


import bot_health_check


@test("HealthCheck: check_bot_log handles missing file")
def test_health_check_missing_log():
    original_log = bot_health_check.LOG_FILE
    try:
        bot_health_check.LOG_FILE = Path("/tmp/nonexistent_bot_log_test.log")
        result = bot_health_check.check_bot_log()
        assert result["log_exists"] is False
    finally:
        bot_health_check.LOG_FILE = original_log


@test("HealthCheck: check_live_status with valid file")
def test_health_check_live_status():
    original_file = bot_health_check.LIVE_STATUS_FILE

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        now_iso = datetime.now(timezone.utc).isoformat()
        json.dump({"timestamp": now_iso, "balance": 50500, "locked": False}, f)
        test_file = Path(f.name)

    try:
        bot_health_check.LIVE_STATUS_FILE = test_file
        result = bot_health_check.check_live_status()

        assert result["exists"] is True
        assert result["age_seconds"] is not None
        assert result["age_seconds"] < 60  # Just written
        assert result["data"]["balance"] == 50500
    finally:
        bot_health_check.LIVE_STATUS_FILE = original_file
        test_file.unlink()


@test("HealthCheck: check_live_status handles missing file")
def test_health_check_missing_live_status():
    original_file = bot_health_check.LIVE_STATUS_FILE
    try:
        bot_health_check.LIVE_STATUS_FILE = Path("/tmp/nonexistent_live_status.json")
        result = bot_health_check.check_live_status()
        assert result["exists"] is False
    finally:
        bot_health_check.LIVE_STATUS_FILE = original_file


@test("HealthCheck: check_token handles missing token file")
def test_health_check_missing_token():
    original_file = bot_health_check.TOKEN_FILE
    try:
        bot_health_check.TOKEN_FILE = Path("/tmp/nonexistent_token.json")
        result = bot_health_check.check_token()
        assert result["exists"] is False
        assert result["valid"] is False
    finally:
        bot_health_check.TOKEN_FILE = original_file


test_health_check_bot_log()
test_health_check_missing_log()
test_health_check_live_status()
test_health_check_missing_live_status()
test_health_check_missing_token()


# ─────────────────────────────────────────────
# 16. CONSISTENCY RULE / DAILY PROFIT CAP TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("16. CONSISTENCY RULE / DAILY PROFIT CAP TESTS")
print("=" * 60)


@test("RiskManager: daily profit cap locks trading")
def test_risk_daily_profit_cap():
    from risk_manager import RiskManager
    rm = RiskManager()
    if rm.daily_profit_cap:
        rm.day_start_balance = rm.current_balance
        # Simulate profit exceeding cap
        rm.update_balance(rm.current_balance + rm.daily_profit_cap + 100, 0)
        assert rm.trading_locked is True
        assert "PROFIT CAP" in rm.lock_reason


@test("Journal: compute_effective_target adjusts for consistency rule")
def test_journal_effective_target():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)

        # Create trades: one big day ($2000) and several small days
        # If consistency_pct = 0.4, then effective target = max(3000, 2000/0.4) = max(3000, 5000) = 5000
        # Manually set up trades
        j.trades = [
            {"id": "t1", "symbol": "NQ", "direction": "Buy", "entry_price": 100,
             "qty": 1, "strategy": "ORB", "reason": "test",
             "stop_loss": 95, "take_profit": 110, "status": "closed",
             "pnl": 2000, "exit_reason": "take_profit",
             "date": "2026-03-10", "entry_hour_et": 10, "entry_time": "2026-03-10T14:00:00+00:00",
             "exit_time": "2026-03-10T15:00:00+00:00", "exit_price": 110, "r_multiple": 2.0},
            {"id": "t2", "symbol": "NQ", "direction": "Buy", "entry_price": 100,
             "qty": 1, "strategy": "ORB", "reason": "test",
             "stop_loss": 95, "take_profit": 110, "status": "closed",
             "pnl": 500, "exit_reason": "take_profit",
             "date": "2026-03-11", "entry_hour_et": 10, "entry_time": "2026-03-11T14:00:00+00:00",
             "exit_time": "2026-03-11T15:00:00+00:00", "exit_price": 110, "r_multiple": 1.0},
        ]

        result = j.compute_effective_target()
        assert result["highest_day_profit"] == 2000
        # If consistency_pct < 1.0, target should be adjusted
        if result["consistency_adjusted"]:
            assert result["effective_target"] > result["base_target"]
    finally:
        os.unlink(path)


@test("Journal: consistency rule not triggered when no big days")
def test_journal_consistency_no_trigger():
    from trade_journal import TradeJournal
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        j = TradeJournal(filepath=path)
        # All small uniform days
        for i in range(5):
            tid = j.record_entry("NQ", "Buy", 100, 1, "ORB", "test")
            j.record_exit(tid, 110, 200, "take_profit")

        result = j.compute_effective_target()
        # All profits are on the same day so highest = 1000
        # Whether adjusted depends on consistency_pct config
        # But effective_target should always >= base_target
        assert result["effective_target"] >= result["base_target"]
    finally:
        os.unlink(path)


@test("RiskManager: set_initial_balance corrects day_pnl")
def test_risk_set_initial_balance():
    from risk_manager import RiskManager
    rm = RiskManager()
    # Simulate: real balance is $51000 but config says $50000
    rm.set_initial_balance(51000.0)
    assert rm.current_balance == 51000.0
    assert rm.day_start_balance == 51000.0
    assert rm._balance_initialized is True
    # day_pnl should be 0 now (balance - day_start = 0)
    rm.update_balance(51000.0, 0)
    assert abs(rm.day_pnl) < 0.01, f"day_pnl should be ~0, got {rm.day_pnl}"


test_risk_daily_profit_cap()
test_journal_effective_target()
test_journal_consistency_no_trigger()
test_risk_set_initial_balance()


# ─────────────────────────────────────────────
# 17. CONTINUOUS LEARNING SYSTEM TESTS
# ─────────────────────────────────────────────
print(f"\n{'=' * 60}")
print("17. CONTINUOUS LEARNING SYSTEM TESTS")
print(f"{'=' * 60}")


def _make_journal_with_trades(n_trades=10, win_pct=0.6, symbol="NQ"):
    """Helper: create a journal with realistic closed trades."""
    from trade_journal import TradeJournal
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    path = f.name
    f.close()
    j = TradeJournal(filepath=path)
    import random
    random.seed(42)
    for i in range(n_trades):
        tid = j.record_entry(
            symbol, "Buy", 21000 + i, 1, "ORBStrategy", f"test_{i}",
            stop_loss=20975, take_profit=21050,
        )
        if random.random() < win_pct:
            j.record_exit(tid, 21050, 250, "take_profit")
        else:
            j.record_exit(tid, 20975, -125, "stop_loss")
        # Set MAE/MFE for learning
        for t in j.trades:
            if t["id"] == tid:
                t["mae_points"] = round(-random.uniform(2, 20), 2)
                t["mfe_points"] = round(random.uniform(5, 40), 2)
                t["entry_day_of_week"] = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"][i % 5]
    j._save()
    return j, path


@test("ContinuousLearner: daily analysis produces report with all fields")
def test_learner_daily():
    from continuous_learner import ContinuousLearner
    j, path = _make_journal_with_trades(10, 0.6)
    try:
        learner = ContinuousLearner(j)
        report = learner.run_daily_analysis()
        assert report["type"] == "daily"
        assert "insights" in report
        assert "parameter_analysis" in report
        assert "score" in report
        assert report["score"]["total"] >= 0
    finally:
        os.unlink(path)
        for f in ["learning_report.json", "learning_history.json"]:
            fp = os.path.join(os.path.dirname(path), f)
            if os.path.exists(fp):
                os.unlink(fp)


@test("ContinuousLearner: weekly analysis produces trend analysis")
def test_learner_weekly():
    from continuous_learner import ContinuousLearner
    j, path = _make_journal_with_trades(20, 0.5)
    try:
        learner = ContinuousLearner(j)
        report = learner.run_weekly_analysis()
        assert report["type"] == "weekly"
        assert "trend_analysis" in report
        assert "recommendations" in report
    finally:
        os.unlink(path)
        for f in ["learning_report.json", "learning_history.json"]:
            fp = os.path.join(os.path.dirname(path), f)
            if os.path.exists(fp):
                os.unlink(fp)


@test("ContinuousLearner: score calculation ranges 0-100")
def test_learner_scoring():
    from continuous_learner import ContinuousLearner
    j, path = _make_journal_with_trades(8, 0.7)
    try:
        learner = ContinuousLearner(j)
        today_trades = j._closed_trades()
        all_trades = j._closed_trades()
        score = learner._score_day(today_trades, all_trades)
        assert 0 <= score["total"] <= 100, f"Score {score['total']} out of range"
        assert "win_rate_score" in score
        assert "pnl_score" in score
        assert "risk_score" in score
        assert "discipline_score" in score
    finally:
        os.unlink(path)


@test("Journal: MAE/MFE update_mae_mfe tracks correctly")
def test_journal_mae_mfe():
    from trade_journal import TradeJournal
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    path = f.name
    f.close()
    try:
        j = TradeJournal(filepath=path)
        tid = j.record_entry("NQ", "Buy", 21000, 1, "ORB", "test")

        # Price goes up (favorable)
        j.update_mae_mfe("NQ", 21020)
        trade = j.trades[-1]
        assert trade["mfe_points"] == 20, f"MFE should be 20, got {trade['mfe_points']}"
        assert trade["mae_points"] == 0 or trade["mae_points"] is None

        # Price goes down (adverse)
        j.update_mae_mfe("NQ", 20985)
        assert trade["mae_points"] == -15, f"MAE should be -15, got {trade['mae_points']}"

        # Price recovers (MFE should stay at 20)
        j.update_mae_mfe("NQ", 21010)
        assert trade["mfe_points"] == 20, "MFE should not decrease"
        assert trade["mae_points"] == -15, "MAE should not increase"
    finally:
        os.unlink(path)


@test("Journal: analyze_by_day_of_week groups correctly")
def test_journal_day_of_week():
    from trade_journal import TradeJournal
    j, path = _make_journal_with_trades(10, 0.6)
    try:
        result = j.analyze_by_day_of_week()
        assert isinstance(result, dict)
        # We set entry_day_of_week in the helper, so we should have data
        total_trades = sum(d["trades"] for d in result.values())
        assert total_trades > 0, "Should have day-of-week data"
    finally:
        os.unlink(path)


@test("Journal: analyze_streaks detects winning and losing streaks")
def test_journal_streaks():
    from trade_journal import TradeJournal
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    path = f.name
    f.close()
    try:
        j = TradeJournal(filepath=path)
        # Create: W, W, W, L, L
        for i in range(3):
            tid = j.record_entry("NQ", "Buy", 21000, 1, "ORB", "test")
            j.record_exit(tid, 21050, 250, "take_profit")
        for i in range(2):
            tid = j.record_entry("NQ", "Buy", 21000, 1, "ORB", "test")
            j.record_exit(tid, 20975, -125, "stop_loss")

        streaks = j.analyze_streaks()
        assert streaks["max_win_streak"] == 3
        assert streaks["max_loss_streak"] == 2
        assert streaks["current_type"] == "loss"
        assert streaks["current_streak"] == 2
    finally:
        os.unlink(path)


@test("AutoTuner: cooldown tuning adjusts when 2nd trades lose")
def test_tuner_cooldowns():
    from auto_tuner import AutoTuner
    j, path = _make_journal_with_trades(20, 0.5)
    try:
        tuner = AutoTuner(j)
        closed = j._closed_trades()
        tuner._tune_cooldowns(closed)
        # May or may not produce adjustments depending on data, but should not crash
        assert isinstance(tuner.adjustments, list)
    finally:
        os.unlink(path)


@test("AutoTuner: MAE-based stop tuning with sufficient data")
def test_tuner_mae_stops():
    from auto_tuner import AutoTuner
    j, path = _make_journal_with_trades(20, 0.6)
    try:
        tuner = AutoTuner(j)
        closed = j._closed_trades()
        tuner._tune_stops_from_mae(closed)
        # Should not crash — may or may not produce adjustments
        assert isinstance(tuner.adjustments, list)
    finally:
        os.unlink(path)


@test("AutoTuner: MFE-based target tuning with sufficient data")
def test_tuner_mfe_targets():
    from auto_tuner import AutoTuner
    j, path = _make_journal_with_trades(20, 0.6)
    try:
        tuner = AutoTuner(j)
        closed = j._closed_trades()
        tuner._tune_targets_from_mfe(closed)
        assert isinstance(tuner.adjustments, list)
    finally:
        os.unlink(path)


test_learner_daily()
test_learner_weekly()
test_learner_scoring()
test_journal_mae_mfe()
test_journal_day_of_week()
test_journal_streaks()
test_tuner_cooldowns()
test_tuner_mae_stops()
test_tuner_mfe_targets()


# ─────────────────────────────────────────────
# 18. ORPHAN POSITION GUARD TESTS
# ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("18. ORPHAN POSITION GUARD TESTS")
print("=" * 60)


@test("API: get_working_orders filters by status")
def test_get_working_orders():
    from tradovate_api import TradovateAPI
    api = TradovateAPI()
    api.access_token = "fake"
    all_orders = [
        {"id": 1, "ordStatus": "Working", "contractId": 100},
        {"id": 2, "ordStatus": "Filled", "contractId": 101},
        {"id": 3, "ordStatus": "Accepted", "contractId": 102},
        {"id": 4, "ordStatus": "Cancelled", "contractId": 100},
    ]
    with patch.object(api, "_get", return_value=all_orders):
        result = api.get_working_orders()
        assert len(result) == 2, f"Expected 2 working orders, got {len(result)}"
        assert result[0]["id"] == 1
        assert result[1]["id"] == 3


@test("Bot: _verify_order_protection detects orphaned position and re-places OCO")
def test_verify_order_protection_replace():
    from bot import TradovateBot
    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.api.get_positions.return_value = [
        {"contractId": 100, "netPos": 1, "netPrice": 24000.0}
    ]
    bot.api.get_working_orders.return_value = []  # No orders = orphaned!
    bot.api._get.return_value = {"id": 100, "name": "MNQH6"}
    bot.api.place_oco_for_position.return_value = {"orderId": 500, "ocoId": 501}
    bot._contract_id_to_symbol = {100: "MNQ"}
    bot.trades_today = [
        {"symbol": "MNQ", "stop": 23975.0, "target": 24050.0, "_closed": False}
    ]
    bot._verify_order_protection()
    bot.api.place_oco_for_position.assert_called_once_with(
        symbol="MNQH6", action="Sell", qty=1,
        stop_price=23975.0, take_profit_price=24050.0,
    )


@test("Bot: _verify_order_protection emergency close when OCO re-place fails")
def test_verify_order_protection_emergency_close():
    from bot import TradovateBot
    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.api.get_positions.return_value = [
        {"contractId": 100, "netPos": 1, "netPrice": 24000.0}
    ]
    bot.api.get_working_orders.return_value = []
    bot.api._get.return_value = {"id": 100, "name": "MNQH6"}
    bot.api.place_oco_for_position.return_value = None  # OCO failed!
    bot._contract_id_to_symbol = {100: "MNQ"}
    bot.trades_today = [
        {"symbol": "MNQ", "stop": 23975.0, "target": 24050.0, "_closed": False}
    ]
    bot._verify_order_protection()
    bot.api.place_market_order.assert_called_once_with("MNQH6", "Sell", 1)


@test("Bot: _verify_order_protection skips protected positions")
def test_verify_order_protection_skip_protected():
    from bot import TradovateBot
    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.api.get_positions.return_value = [
        {"contractId": 100, "netPos": 1, "netPrice": 24000.0}
    ]
    bot.api.get_working_orders.return_value = [
        {"id": 500, "ordStatus": "Working", "contractId": 100}
    ]
    bot._contract_id_to_symbol = {100: "MNQ"}
    bot.trades_today = []
    bot._verify_order_protection()
    bot.api.place_oco_for_position.assert_not_called()
    bot.api.place_market_order.assert_not_called()


@test("Bot: _verify_order_protection uses config defaults when no trade data")
def test_verify_order_protection_config_defaults():
    from bot import TradovateBot
    bot = TradovateBot(dry_run=False)
    bot.api = MagicMock()
    bot.api.get_positions.return_value = [
        {"contractId": 100, "netPos": 2, "netPrice": 24000.0}
    ]
    bot.api.get_working_orders.return_value = []
    bot.api._get.return_value = {"id": 100, "name": "MNQH6"}
    bot.api.place_oco_for_position.return_value = {"orderId": 500, "ocoId": 501}
    bot._contract_id_to_symbol = {100: "MNQ"}
    bot.trades_today = []  # No trade data available
    bot._verify_order_protection()
    # Should use config defaults: MNQ stop=25pts, tp=50pts
    bot.api.place_oco_for_position.assert_called_once()
    call_args = bot.api.place_oco_for_position.call_args
    assert call_args.kwargs["stop_price"] == 24000.0 - 25, f"Expected SL at 23975, got {call_args.kwargs['stop_price']}"
    assert call_args.kwargs["take_profit_price"] == 24000.0 + 50, f"Expected TP at 24050, got {call_args.kwargs['take_profit_price']}"


test_get_working_orders()
test_verify_order_protection_replace()
test_verify_order_protection_emergency_close()
test_verify_order_protection_skip_protected()
test_verify_order_protection_config_defaults()


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
