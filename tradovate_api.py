"""
Tradovate API Client
=====================
REST + WebSocket wrapper for Tradovate.
Handles authentication, order placement (bracket orders),
account info, positions, and market data subscriptions.

Based on the Tradovate REST v1 API and their custom WebSocket protocol.
References:
  - https://api.tradovate.com/
  - https://github.com/tradovate/example-api-faq
  - https://github.com/cullen-b/Tradovate-Python-Client
"""

import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests
import websocket

import config

# File for persisting auth tokens between restarts
_TOKEN_FILE = Path(__file__).parent / ".tradovate_token.json"

# ─────────────────────────────────────────────
# Tradovate Web Auth Helpers
# ─────────────────────────────────────────────
# Reverse-engineered from the Tradovate web trader JS bundle.
# The web app encrypts the password and computes an HMAC before
# sending the auth request. This lets us authenticate using only
# username + password (no CID/Secret needed).

_HMAC_KEY = "1259-11e7-485a-aeae-9b6016579351"
_WEB_APP_ID = "tradovate_trader(web)"
_WEB_APP_VERSION = "3.260220.0"
_HMAC_FIELDS = ["chl", "deviceId", "name", "password", "appId"]


def _encrypt_password(name: str, password: str) -> str:
    """Tradovate's client-side password encoding (btoa of shifted+reversed)."""
    offset = len(name) % len(password)
    rearranged = password[offset:] + password[:offset]
    reversed_pw = rearranged[::-1]
    return base64.b64encode(reversed_pw.encode()).decode()


def _compute_hmac_sec(payload: dict) -> str:
    """Compute the HMAC-SHA256 'sec' field from the auth payload."""
    message = "".join(str(payload.get(f, "")) for f in _HMAC_FIELDS)
    return hmac_mod.new(
        _HMAC_KEY.encode(), message.encode(), hashlib.sha256
    ).hexdigest()

logger = logging.getLogger(__name__)


class TradovateAPI:
    """Synchronous REST client for Tradovate."""

    def __init__(self):
        self.base_url: str = config.REST_URL
        self.access_token: Optional[str] = None
        self.md_access_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        self.user_id: Optional[int] = None
        self.account_id: Optional[int] = None
        self.account_spec: Optional[str] = None

    # ─────────────────────────────────────────
    # Authentication
    # ─────────────────────────────────────────

    def set_token(
        self,
        access_token: str,
        md_access_token: Optional[str] = None,
        user_id: Optional[int] = None,
        expiration_time: Optional[str] = None,
    ):
        """
        Inject auth tokens from an external source (e.g. browser login).
        Call this before authenticate() to skip the CID/Secret auth flow.
        """
        self.access_token = access_token
        self.md_access_token = md_access_token
        self.user_id = user_id
        if expiration_time:
            self.token_expiry = datetime.fromisoformat(
                expiration_time.replace("Z", "+00:00")
            )
        logger.info("External tokens injected (userId=%s)", self.user_id)

    def authenticate(self) -> bool:
        """
        Obtain access tokens from Tradovate.

        Auth priority:
        1. Pre-injected token (via set_token())
        2. Saved token from previous session (auto-renewed)
        3. Web-style auth (username + password, no CID)
        4. API-key auth (CID + Secret)

        Returns True on success.
        """
        # 0. Token from environment variable (manual override)
        if config.TRADOVATE_ACCESS_TOKEN and not self.access_token:
            logger.info("Using token from TRADOVATE_ACCESS_TOKEN env var")
            self.access_token = config.TRADOVATE_ACCESS_TOKEN

        # 1. Pre-injected token
        if self.access_token:
            logger.info("Using pre-injected auth token")
            self._fetch_account_id()
            self._save_token()
            return True

        # 2. Try saved token from file
        if self._load_token():
            logger.info("Loaded saved token, attempting renewal...")
            if self.renew_token():
                logger.info("Saved token renewed successfully")
                self._fetch_account_id()
                self._save_token()
                return True
            logger.warning("Saved token expired, trying fresh auth...")

        url = f"{self.base_url}/auth/accesstokenrequest"

        # 3. Web-style auth (no CID/Secret needed)
        data = self._try_web_auth(url)
        # 4. API-key auth
        if data is None:
            data = self._try_api_auth(url)
        if data is None:
            return False

        if "accessToken" not in data:
            logger.error("No accessToken in response: %s", data)
            return False

        self.access_token = data["accessToken"]
        self.md_access_token = data.get("mdAccessToken")
        self.user_id = data.get("userId")
        self.account_spec = data.get("name")

        if data.get("expirationTime"):
            self.token_expiry = datetime.fromisoformat(
                data["expirationTime"].replace("Z", "+00:00")
            )

        logger.info(
            "Authenticated as %s (userId=%s)", self.account_spec, self.user_id
        )

        self._fetch_account_id()
        self._save_token()
        return True

    # ── Token persistence ──

    def _save_token(self):
        """Save current auth tokens to file for reuse between restarts."""
        if not self.access_token:
            return
        data = {
            "accessToken": self.access_token,
            "mdAccessToken": self.md_access_token,
            "userId": self.user_id,
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "expirationTime": self.token_expiry.isoformat() if self.token_expiry else None,
            "savedAt": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _TOKEN_FILE.write_text(json.dumps(data, indent=2))
            logger.debug("Token saved to %s", _TOKEN_FILE)
        except OSError as e:
            logger.warning("Could not save token: %s", e)

    def _load_token(self) -> bool:
        """Load saved auth tokens from file. Returns True if loaded."""
        if not _TOKEN_FILE.exists():
            return False
        try:
            data = json.loads(_TOKEN_FILE.read_text())
            self.access_token = data.get("accessToken")
            self.md_access_token = data.get("mdAccessToken")
            self.user_id = data.get("userId")
            self.account_spec = data.get("accountSpec")
            self.account_id = data.get("accountId")
            if data.get("expirationTime"):
                self.token_expiry = datetime.fromisoformat(data["expirationTime"])
            return bool(self.access_token)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load saved token: %s", e)
            return False

    def _try_web_auth(self, url: str) -> Optional[dict]:
        """
        Authenticate using the same mechanism as the Tradovate web trader.
        No CID/Secret required — just username, password, and organization.
        """
        name = config.TRADOVATE_USERNAME
        password = config.TRADOVATE_PASSWORD
        if not name or not password:
            return None

        payload = {
            "name": name,
            "password": password,
            "appId": _WEB_APP_ID,
            "appVersion": _WEB_APP_VERSION,
            "deviceId": config.TRADOVATE_DEVICE_ID,
            "cid": 8,
            "sec": "",
            # Always include organization — some prop firms (e.g. FundedNext)
            # require an empty string; omitting the field entirely fails.
            "organization": config.TRADOVATE_ORGANIZATION,
        }

        try:
            org = config.TRADOVATE_ORGANIZATION
            logger.info(
                "Trying web-style authentication (no CID needed)%s...",
                f" org=\"{org}\"" if org else "",
            )
            resp = requests.post(url, json=payload, timeout=30)
            data = resp.json()
            if "accessToken" in data:
                logger.info("Web auth succeeded")
                return data

            # Handle p-ticket (device verification / CAPTCHA required)
            if "p-ticket" in data:
                return self._handle_p_ticket(url, data, payload)

            error = data.get("errorText", "")
            logger.info("Web auth response: %s", error)
        except requests.RequestException as e:
            logger.warning("Web auth request failed: %s", e)
        return None

    def _handle_p_ticket(
        self, url: str, ticket_data: dict, original_payload: dict
    ) -> Optional[dict]:
        """
        Handle Tradovate's p-ticket device verification flow.

        On first login from a new device, Tradovate returns a p-ticket
        and may require CAPTCHA. For headless operation, we attempt to
        complete verification without CAPTCHA. If CAPTCHA is required,
        the user must obtain a token via browser.
        """
        p_ticket = ticket_data["p-ticket"]
        p_time = ticket_data.get("p-time", 15)
        needs_captcha = ticket_data.get("p-captcha", False)

        logger.info(
            "Credentials accepted! Device verification required "
            "(p-ticket received, captcha=%s, timeout=%ss)",
            needs_captcha,
            p_time,
        )

        if needs_captcha:
            logger.warning(
                "\n"
                "╔══════════════════════════════════════════════════════════╗\n"
                "║  CAPTCHA REQUIRED — one-time setup needed                ║\n"
                "║                                                          ║\n"
                "║  Credentials are CORRECT but Tradovate requires           ║\n"
                "║  reCAPTCHA v2 (sitekey: 6Ld7FAoTAAAAA...) on first       ║\n"
                "║  login from a new device.                                 ║\n"
                "║                                                          ║\n"
                "║  To fix (choose one):                                     ║\n"
                "║                                                          ║\n"
                "║  Option 1 (easiest): Run get_token.py on your PC         ║\n"
                "║    $ python get_token.py                                 ║\n"
                "║                                                          ║\n"
                "║  Option 2: Get token from browser DevTools:              ║\n"
                "║    1. Log into https://trader.tradovate.com              ║\n"
                "║    2. Open DevTools (F12) → Network tab                  ║\n"
                "║    3. Filter by 'Fetch/XHR', click any request           ║\n"
                "║    4. In Headers, copy the 'Authorization: Bearer ...'   ║\n"
                "║    5. Paste token into .env:                             ║\n"
                "║       TRADOVATE_ACCESS_TOKEN=<paste_here>                ║\n"
                "║                                                          ║\n"
                "║  After first setup, the bot auto-renews the token.       ║\n"
                "╚══════════════════════════════════════════════════════════╝"
            )

        # Wait for p-time and attempt verification with just the ticket
        import time as _time
        logger.info("Waiting %d seconds before verification attempt...", p_time)
        _time.sleep(p_time + 1)

        verify_payload = dict(original_payload)
        verify_payload["p-ticket"] = p_ticket

        try:
            resp = requests.post(url, json=verify_payload, timeout=30)
            data = resp.json()
            if "accessToken" in data:
                logger.info("Device verification succeeded!")
                return data
            if "p-ticket" in data:
                logger.info(
                    "Verification still pending (captcha required). "
                    "Use get_token.py or browser to obtain initial token."
                )
            else:
                logger.info("Verification response: %s", data.get("errorText", data))
        except requests.RequestException as e:
            logger.warning("Verification request failed: %s", e)

        return None

    def _try_api_auth(self, url: str) -> Optional[dict]:
        """Authenticate using traditional API key auth (CID + Secret)."""
        if not config.TRADOVATE_SECRET:
            logger.info("No API secret configured, skipping API-key auth")
            return None

        payload = {
            "name": config.TRADOVATE_USERNAME,
            "password": config.TRADOVATE_PASSWORD,
            "appId": config.TRADOVATE_APP_ID or _WEB_APP_ID,
            "appVersion": "1.0",
            "cid": config.TRADOVATE_CID,
            "sec": config.TRADOVATE_SECRET,
            "deviceId": config.TRADOVATE_DEVICE_ID,
            "organization": config.TRADOVATE_ORGANIZATION,
        }

        try:
            logger.info("Trying API-key authentication...")
            resp = requests.post(url, json=payload, timeout=30)
            data = resp.json()
            if "accessToken" in data:
                logger.info("API-key auth succeeded")
                return data
            if "p-ticket" in data:
                return self._handle_p_ticket(url, data, payload)
            error = data.get("errorText", "")
            logger.error("API-key auth failed: %s", error)
        except requests.RequestException as e:
            logger.error("API-key auth request failed: %s", e)
        return None

    def renew_token(self) -> bool:
        """Renew the access token before it expires."""
        url = f"{self.base_url}/auth/renewaccesstoken"
        try:
            resp = requests.post(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self.access_token = data.get("accessToken", self.access_token)
            if data.get("expirationTime"):
                self.token_expiry = datetime.fromisoformat(
                    data["expirationTime"].replace("Z", "+00:00")
                )
            logger.info("Token renewed. Expires: %s", self.token_expiry)
            self._save_token()
            return True
        except requests.RequestException as e:
            logger.error("Token renewal failed: %s", e)
            return False

    def ensure_token_valid(self):
        """Renew token if close to expiry."""
        if self.token_expiry is None:
            return
        now = datetime.now(timezone.utc)
        # Renew if less than 5 minutes remain
        if (self.token_expiry - now).total_seconds() < 300:
            self.renew_token()

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }

    def _fetch_account_id(self):
        """Get the first account ID."""
        accounts = self.get_accounts()
        if accounts:
            self.account_id = accounts[0]["id"]
            self.account_spec = accounts[0].get("name", self.account_spec)
            logger.info("Account ID: %s (%s)", self.account_id, self.account_spec)

    # ─────────────────────────────────────────
    # Account & Position queries
    # ─────────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        """List all accounts."""
        return self._get("/account/list") or []

    def get_positions(self) -> list[dict]:
        """List all open positions."""
        return self._get("/position/list") or []

    def get_cash_balance(self) -> Optional[dict]:
        """Get cash balance snapshot for the active account."""
        if self.account_id is None:
            return None
        return self._post(
            "/cashBalance/getcashbalancesnapshot",
            {"accountId": self.account_id},
        )

    def get_fills(self) -> list[dict]:
        """List recent fills."""
        return self._get("/fill/list") or []

    # ─────────────────────────────────────────
    # Contract lookup
    # ─────────────────────────────────────────

    def find_contract(self, symbol: str) -> Optional[dict]:
        """
        Find a contract by symbol name (e.g. 'NQM5', 'ESH6').
        Returns the contract dict or None.
        """
        result = self._get(f"/contract/find?name={symbol}")
        return result if result else None

    def suggest_contract(self, base_symbol: str) -> Optional[dict]:
        """
        Get the front-month contract for a base symbol (e.g. 'NQ', 'ES').
        Uses the /contract/suggest endpoint to find the most liquid contract.
        """
        result = self._get(f"/contract/suggest?t={base_symbol}&l=1")
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
        return None

    # ─────────────────────────────────────────
    # Order placement
    # ─────────────────────────────────────────

    def place_bracket_order(
        self,
        symbol: str,
        action: str,
        qty: int,
        entry_price: Optional[float],
        stop_price: float,
        take_profit_price: float,
        order_type: str = "Market",
    ) -> Optional[dict]:
        """
        Place an OSO bracket order: entry + stop loss + take profit.

        Args:
            symbol: Contract symbol (e.g. 'NQM5')
            action: 'Buy' or 'Sell'
            qty: Number of contracts
            entry_price: Limit price for entry (None for market orders)
            stop_price: Stop loss price
            take_profit_price: Take profit limit price
            order_type: 'Market' or 'Limit'
        """
        opposite_action = "Sell" if action == "Buy" else "Buy"

        payload: dict[str, Any] = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,
            "timeInForce": "Day",
            "isAutomated": True,
            "bracket1": {
                "action": opposite_action,
                "orderType": "Stop",
                "stopPrice": stop_price,
            },
            "bracket2": {
                "action": opposite_action,
                "orderType": "Limit",
                "price": take_profit_price,
            },
        }

        if order_type == "Limit" and entry_price is not None:
            payload["price"] = entry_price

        logger.info(
            "Placing bracket %s %d %s @ %s | SL=%.2f TP=%.2f",
            action,
            qty,
            symbol,
            order_type,
            stop_price,
            take_profit_price,
        )

        return self._post("/order/placeOSO", payload)

    def place_market_order(
        self, symbol: str, action: str, qty: int
    ) -> Optional[dict]:
        """Place a simple market order (no brackets)."""
        payload = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "timeInForce": "Day",
            "isAutomated": True,
        }
        return self._post("/order/placeorder", payload)

    def cancel_all_orders(self) -> bool:
        """Cancel all working orders for the account."""
        orders = self._get("/order/list") or []
        cancelled = 0
        for order in orders:
            if order.get("ordStatus") in ("Working", "Accepted"):
                self._post("/order/cancelorder", {"orderId": order["id"]})
                cancelled += 1
        logger.info("Cancelled %d working orders", cancelled)
        return True

    def close_all_positions(self) -> bool:
        """Flatten all open positions."""
        positions = self.get_positions()
        for pos in positions:
            net_pos = pos.get("netPos", 0)
            if net_pos == 0:
                continue
            action = "Sell" if net_pos > 0 else "Buy"
            qty = abs(net_pos)
            symbol = pos.get("contractId")
            self.place_market_order(str(symbol), action, qty)
            logger.info("Closing position: %s %d on contractId %s", action, qty, symbol)
        return True

    # ─────────────────────────────────────────
    # HTTP helpers
    # ─────────────────────────────────────────

    def _get(self, endpoint: str) -> Any:
        self.ensure_token_valid()
        try:
            resp = requests.get(
                f"{self.base_url}{endpoint}", headers=self._headers(), timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("GET %s failed: %s", endpoint, e)
            return None

    def _post(self, endpoint: str, payload: dict) -> Any:
        self.ensure_token_valid()
        try:
            resp = requests.post(
                f"{self.base_url}{endpoint}",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("POST %s failed: %s", endpoint, e)
            return None


# ─────────────────────────────────────────────
# Market Data WebSocket
# ─────────────────────────────────────────────


class MarketDataStream:
    """
    WebSocket client for Tradovate market data.

    Protocol (reverse-engineered from Tradovate web trader JS):
      - Transport: raw WebSocket to wss://md.tradovateapi.com/v1/websocket
      - On connect, server sends "o" (open frame)
      - Client sends: "authorize\\n<id>\\n\\n<token>" to authenticate
      - Server responds: 'a[{"i":<id>,"s":200,...}]' on success
      - Heartbeat: server sends "h" periodically; client should reply with "[]"
      - Data frames: 'a[{...},{...}]' — JSON array of event objects
      - Subscriptions: "md/subscribeQuote\\n<id>\\n\\n{\"symbol\":\"NQH6\"}"
    """

    # Reconnect settings
    MAX_RECONNECT_ATTEMPTS = 5
    RECONNECT_BASE_DELAY = 2  # seconds

    def __init__(self, md_access_token: str):
        self.md_token = md_access_token
        self.ws: Optional[websocket.WebSocketApp] = None
        self._request_id = 0
        self._callbacks: dict[str, list[Callable]] = {}
        self._connected = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._should_run = False
        self._reconnect_count = 0

    def start(self):
        """Connect and start listening in a background thread."""
        self._should_run = True
        self._connect()
        self._connected.wait(timeout=15)

    def _connect(self):
        """Create WebSocket and connect."""
        self.ws = websocket.WebSocketApp(
            config.WS_MARKET_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        """Close the WebSocket."""
        self._should_run = False
        if self.ws:
            self.ws.close()

    def subscribe_quote(self, symbol: str, callback: Callable):
        """Subscribe to real-time quotes for a symbol."""
        self._callbacks.setdefault(symbol, []).append(callback)
        self._send("md/subscribeQuote", {"symbol": symbol})
        logger.info("Subscribed to quotes: %s", symbol)

    def unsubscribe_quote(self, symbol: str):
        """Unsubscribe from quotes."""
        self._send("md/unsubscribeQuote", {"symbol": symbol})
        self._callbacks.pop(symbol, None)

    def on_quote(self, symbol: str, callback: Callable):
        """Register a callback for quote updates on a symbol."""
        self._callbacks.setdefault(symbol, []).append(callback)

    # ─────── WebSocket handlers ───────

    def _on_open(self, ws):
        logger.info("Market data WebSocket connected")

    def _on_message(self, ws, message: str):
        if message == "o":
            # Connection opened, send auth
            auth_msg = f"authorize\n1\n\n{self.md_token}"
            ws.send(auth_msg)
            return

        if message == "h":
            # Heartbeat — reply to keep connection alive
            ws.send("[]")
            return

        if message.startswith("a"):
            try:
                payload = json.loads(message[1:])
                self._handle_payload(payload)
            except json.JSONDecodeError:
                pass
            return

    def _handle_payload(self, payload: list):
        """Process incoming data frames."""
        for item in payload:
            if not isinstance(item, dict):
                continue

            # Auth response
            if item.get("i") == 1 and item.get("s") == 200:
                logger.info("Market data WebSocket authenticated")
                self._reconnect_count = 0
                self._connected.set()
                continue

            # Auth failure
            if item.get("i") == 1 and item.get("s") != 200:
                logger.error("Market data auth failed: %s", item)
                continue

            # Quote data — dispatched by symbol from the "d" field
            if "e" in item and item["e"] == "md" and "d" in item:
                data = item["d"]
                quotes = data.get("quotes", [data]) if isinstance(data, dict) else [data]
                for quote in quotes:
                    contract_id = quote.get("contractId")
                    for sym, cbs in self._callbacks.items():
                        for cb in cbs:
                            try:
                                cb(sym, quote)
                            except Exception as e:
                                logger.error("Quote callback error: %s", e)

    def _on_error(self, ws, error):
        logger.error("Market data WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning("Market data WebSocket closed: %s %s", close_status_code, close_msg)
        self._connected.clear()
        # Auto-reconnect
        if self._should_run and self._reconnect_count < self.MAX_RECONNECT_ATTEMPTS:
            self._reconnect_count += 1
            delay = self.RECONNECT_BASE_DELAY * (2 ** (self._reconnect_count - 1))
            logger.info(
                "Reconnecting in %ds (attempt %d/%d)...",
                delay, self._reconnect_count, self.MAX_RECONNECT_ATTEMPTS,
            )
            reconnect_timer = threading.Timer(delay, self._reconnect)
            reconnect_timer.daemon = True
            reconnect_timer.start()

    def _reconnect(self):
        """Reconnect and re-subscribe to all symbols."""
        self._connect()
        if self._connected.wait(timeout=15):
            for symbol in list(self._callbacks.keys()):
                self._send("md/subscribeQuote", {"symbol": symbol})
                logger.info("Re-subscribed to: %s", symbol)

    def _send(self, endpoint: str, body: dict):
        """Send a message using the Tradovate WebSocket protocol."""
        self._request_id += 1
        msg = f"{endpoint}\n{self._request_id}\n\n{json.dumps(body)}"
        if self.ws:
            self.ws.send(msg)
