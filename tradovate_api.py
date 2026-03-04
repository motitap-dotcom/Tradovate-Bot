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
            # Always attempt renewal — even for expired tokens.
            # Tradovate may accept renewal for recently-expired tokens.
            # Only skip if there's literally no token string.
            logger.info("Loaded saved token, attempting renewal...")
            if self.renew_token():
                logger.info("Saved token renewed successfully")
                self._fetch_account_id()
                self._save_token()
                return True
            logger.warning("Saved token renewal failed, trying fresh auth...")
            # Clear stale token and delete file before fresh auth
            self.access_token = None
            self.md_access_token = None
            try:
                _TOKEN_FILE.unlink(missing_ok=True)
            except OSError:
                pass

        url = f"{self.base_url}/auth/accesstokenrequest"
        live_url = "https://live.tradovateapi.com/v1/auth/accesstokenrequest"

        # 3. Web-style auth — try live endpoint first (FundedNext works better on live)
        logger.info("Trying web auth on live endpoint first...")
        data = self._try_web_auth(live_url)
        # 3b. If live failed, try demo endpoint
        if data is None and "demo" in self.base_url:
            logger.info("Trying web auth on demo endpoint...")
            data = self._try_web_auth(url)
        # 4. API-key auth
        if data is None:
            data = self._try_api_auth(url)
        # 5. Direct browser login (handles CAPTCHA automatically)
        if data is None:
            data = self._try_browser_auth()
        if data is None:
            logger.error("All authentication methods exhausted")
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
        Password is encrypted and HMAC sec is computed to match the web app.
        """
        name = config.TRADOVATE_USERNAME
        password = config.TRADOVATE_PASSWORD
        if not name or not password:
            return None

        encrypted_pw = _encrypt_password(name, password)
        payload = {
            "name": name,
            "password": encrypted_pw,
            "appId": _WEB_APP_ID,
            "appVersion": _WEB_APP_VERSION,
            "deviceId": config.TRADOVATE_DEVICE_ID,
            "cid": 8,
            "sec": "",
            "chl": "",
            # Always include organization — some prop firms (e.g. FundedNext)
            # require an empty string; omitting the field entirely fails.
            "organization": config.TRADOVATE_ORGANIZATION,
        }
        payload["sec"] = _compute_hmac_sec(payload)

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
            # Tradovate returns "Incorrect password" when rate-limited
            if "Incorrect" in error and "p-ticket" not in str(data):
                logger.warning(
                    "Web auth: '%s' — may be rate-limited. "
                    "Waiting 20s before next attempt...", error,
                )
                time.sleep(20)
            else:
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
            logger.info(
                "CAPTCHA required for device verification. "
                "Attempting browser-based login..."
            )
            # Try automated browser login (bypasses CAPTCHA)
            browser_data = self._try_browser_auth()
            if browser_data:
                return browser_data

            logger.warning(
                "\n"
                "╔══════════════════════════════════════════════════════════╗\n"
                "║  CAPTCHA REQUIRED — browser auto-login also failed       ║\n"
                "║                                                          ║\n"
                "║  To fix: Run get_token.py on a machine with a display   ║\n"
                "║    $ python get_token.py                                 ║\n"
                "║                                                          ║\n"
                "║  Or get token from browser DevTools:                     ║\n"
                "║    1. Log into https://trader.tradovate.com              ║\n"
                "║    2. Open DevTools (F12) → Network tab                  ║\n"
                "║    3. Copy 'Authorization: Bearer ...' header            ║\n"
                "║    4. Paste into .env: TRADOVATE_ACCESS_TOKEN=<token>    ║\n"
                "║                                                          ║\n"
                "║  After first setup, the bot auto-renews the token.       ║\n"
                "╚══════════════════════════════════════════════════════════╝"
            )
            return None

        # No CAPTCHA needed — wait for p-time and verify with ticket
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

    def _try_browser_auth(self) -> Optional[dict]:
        """
        Authenticate via headless browser (Playwright).

        Uses the actual Tradovate web login page. This bypasses the API
        CAPTCHA requirement because a real browser session is used.
        Requires playwright to be installed.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.info("Playwright not installed, skipping browser auth")
            return None

        # Detect proxy from environment
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        proxy_cfg = None
        if proxy_url:
            import re as _re
            m = _re.match(r"http://([^:]+):([^@]+)@([^:]+):(\d+)", proxy_url)
            if m:
                proxy_cfg = {
                    "server": f"http://{m.group(3)}:{m.group(4)}",
                    "username": m.group(1),
                    "password": m.group(2),
                }

        # Web trader login page is the same for both environments
        trader_url = "https://trader.tradovate.com"

        captured: dict = {}

        def _on_response(response):
            if captured:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                data = response.json()
                if isinstance(data, dict) and "accessToken" in data:
                    captured.update(data)
            except Exception:
                pass

        # Retry browser auth up to 2 times (page load can be flaky)
        for attempt in range(1, 3):
            logger.info("Attempting browser-based login at %s (attempt %d/2)...", trader_url, attempt)
            browser = None
            try:
                with sync_playwright() as pw:
                    launch_args = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-blink-features=AutomationControlled",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-extensions",
                            "--disable-software-rasterizer",
                            "--disable-background-networking",
                            "--js-flags=--max-old-space-size=256",
                        ],
                    }
                    if proxy_cfg:
                        launch_args["proxy"] = proxy_cfg

                    logger.info("Launching Chromium (headless)...")
                    browser = pw.chromium.launch(**launch_args)
                    ctx = browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/121.0.0.0 Safari/537.36"
                        ),
                        ignore_https_errors=True,
                    )
                    ctx.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    )
                    page = ctx.new_page()
                    page.on("response", _on_response)

                    logger.info("Loading %s ...", trader_url)
                    page.goto(trader_url, timeout=60000, wait_until="domcontentloaded")
                    logger.info("Page loaded. Title: %s. Waiting for login form...", page.title())
                    page.wait_for_timeout(10000)

                    # Fill login form
                    text_input = page.query_selector('input[type="text"]')
                    pass_input = page.query_selector('input[type="password"]')
                    if text_input and pass_input:
                        logger.info("Login form found. Filling credentials...")
                        text_input.fill(config.TRADOVATE_USERNAME)
                        pass_input.fill(config.TRADOVATE_PASSWORD)
                        page.wait_for_timeout(500)

                        # Click login button
                        clicked = False
                        for btn in page.query_selector_all("button"):
                            btn_text = (btn.inner_text() or "").strip().lower()
                            if "login" in btn_text or "sign in" in btn_text or "log in" in btn_text:
                                logger.info("Clicking login button: '%s'", btn_text)
                                btn.click()
                                clicked = True
                                break
                        if not clicked:
                            logger.info("No login button found, pressing Enter")
                            page.keyboard.press("Enter")

                        # Wait for token capture (up to 60 seconds)
                        logger.info("Waiting for auth response (up to 60s)...")
                        for i in range(60):
                            if captured:
                                break
                            page.wait_for_timeout(1000)
                            if i == 15:
                                # Log page state at 15s for debugging
                                cur_url = page.url
                                logger.info("Still waiting... current URL: %s", cur_url)
                    else:
                        logger.warning(
                            "Browser auth: login form not found. URL: %s, Title: %s",
                            page.url, page.title(),
                        )
                        # Try to find any input fields for debugging
                        inputs = page.query_selector_all("input")
                        logger.info("Found %d input elements on page", len(inputs))

                    browser.close()
                    browser = None

                if captured and "accessToken" in captured:
                    logger.info("Browser auth succeeded! userId=%s", captured.get("userId"))
                    return captured
                logger.warning("Browser auth attempt %d: no token captured", attempt)
            except Exception as e:
                logger.warning("Browser auth attempt %d failed: %s", attempt, e)
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass

            if attempt < 2:
                import time as _t
                _t.sleep(10)

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
        """Renew the access token before it expires.

        Tries the configured base_url first, then falls back to the other
        environment (live↔demo) since the token may have been issued there.
        """
        urls = [f"{self.base_url}/auth/renewaccesstoken"]
        # Add the other environment as fallback
        if "demo" in self.base_url:
            urls.append("https://live.tradovateapi.com/v1/auth/renewaccesstoken")
        else:
            urls.append("https://demo.tradovateapi.com/v1/auth/renewaccesstoken")

        for url in urls:
            try:
                resp = requests.post(url, headers=self._headers(), timeout=30)
                resp.raise_for_status()
                data = resp.json()
                self.access_token = data.get("accessToken", self.access_token)
                if data.get("expirationTime"):
                    self.token_expiry = datetime.fromisoformat(
                        data["expirationTime"].replace("Z", "+00:00")
                    )
                logger.info("Token renewed via %s. Expires: %s", url.split("/")[2], self.token_expiry)
                self._save_token()
                return True
            except requests.RequestException as e:
                logger.warning("Token renewal failed on %s: %s", url.split("/")[2], e)
        return False

    def ensure_token_valid(self):
        """Renew token if close to expiry. Falls back to full re-auth if renewal fails."""
        if self.token_expiry is None:
            return
        now = datetime.now(timezone.utc)
        remaining = (self.token_expiry - now).total_seconds()
        # Renew if less than 10 minutes remain (was 5 — more buffer for slow networks)
        if remaining < 600:
            if remaining <= 0:
                logger.warning("Token EXPIRED (%.0fs ago). Attempting full re-auth...", -remaining)
            else:
                logger.info("Token expiring in %.0fs. Renewing...", remaining)
            if not self.renew_token():
                logger.warning("Token renewal failed. Attempting full re-authentication...")
                # Clear expired token so authenticate() doesn't short-circuit
                old_token = self.access_token
                self.access_token = None
                self.md_access_token = None
                if not self.authenticate():
                    # Restore old token as last resort (might still work for a bit)
                    self.access_token = old_token
                    logger.error("Full re-authentication also failed!")

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }

    def _fetch_account_id(self):
        """Get the first account ID. Tries alternate endpoint if none found."""
        accounts = self.get_accounts()
        if not accounts:
            # Challenge accounts (e.g. FundedNext) live on demo even if auth
            # succeeded on live.  Try the other endpoint before giving up.
            if "demo" in self.base_url:
                alt = "https://live.tradovateapi.com/v1"
            else:
                alt = "https://demo.tradovateapi.com/v1"
            logger.warning(
                "No accounts on %s — trying %s...",
                self.base_url.split("/")[2], alt.split("/")[2],
            )
            try:
                resp = requests.get(
                    f"{alt}/account/list", headers=self._headers(), timeout=30
                )
                if resp.status_code == 200:
                    accounts = resp.json()
                    if accounts:
                        # Switch base_url to the endpoint that has the account
                        old_url = self.base_url
                        self.base_url = alt
                        logger.info(
                            "Found accounts on %s — switching base_url from %s",
                            alt.split("/")[2], old_url.split("/")[2],
                        )
            except requests.RequestException as e:
                logger.warning("Alternate endpoint account lookup failed: %s", e)

        if accounts:
            self.account_id = accounts[0]["id"]
            self.account_spec = accounts[0].get("name", self.account_spec)
            logger.info("Account ID: %s (%s)", self.account_id, self.account_spec)
        else:
            logger.error(
                "No accounts found on any endpoint! account_id remains None. "
                "Balance sync and order placement will not work."
            )

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
        Place a bracket order: market/limit entry + OCO stop-loss & take-profit.

        Uses placeorder (entry) + placeOCO (SL/TP) because FundedNext
        blocks the placeOSO endpoint.
        """
        opposite_action = "Sell" if action == "Buy" else "Buy"

        # --- Step 1: Entry order ---
        entry_payload: dict[str, Any] = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,
            "timeInForce": "Day",
            "isAutomated": True,
        }
        if order_type == "Limit" and entry_price is not None:
            entry_payload["price"] = entry_price

        logger.info(
            "Placing bracket %s %d %s @ %s | SL=%.2f TP=%.2f",
            action, qty, symbol, order_type, stop_price, take_profit_price,
        )

        entry_result = self._post("/order/placeorder", entry_payload)
        if not entry_result or "orderId" not in entry_result:
            logger.error("Entry order failed: %s", entry_result)
            return None

        entry_order_id = entry_result["orderId"]
        logger.info("Entry order placed: orderId=%s", entry_order_id)

        # --- Step 2: OCO stop-loss + take-profit ---
        oco_payload: dict[str, Any] = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "symbol": symbol,
            "action": opposite_action,
            "orderQty": qty,
            "orderType": "Stop",
            "stopPrice": stop_price,
            "timeInForce": "GTC",
            "isAutomated": True,
            "other": {
                "action": opposite_action,
                "orderType": "Limit",
                "price": take_profit_price,
                "orderQty": qty,
                "timeInForce": "GTC",
            },
        }

        oco_result = self._post("/order/placeOCO", oco_payload)
        if not oco_result or "orderId" not in oco_result:
            logger.error("OCO (SL/TP) order failed: %s | entry was %s", oco_result, entry_order_id)
        else:
            logger.info(
                "OCO placed: SL orderId=%s TP orderId=%s",
                oco_result.get("orderId"), oco_result.get("ocoId"),
            )

        return {
            "orderId": entry_order_id,
            "slOrderId": oco_result.get("orderId") if oco_result else None,
            "tpOrderId": oco_result.get("ocoId") if oco_result else None,
        }

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
            contract_id = pos.get("contractId")
            # Look up contract name from ID (placeorder needs the name, not the numeric ID)
            contract = self._get(f"/contract/item?id={contract_id}")
            if contract and contract.get("name"):
                symbol = contract["name"]
            else:
                logger.error("Could not resolve contractId %s to name, skipping", contract_id)
                continue
            self.place_market_order(symbol, action, qty)
            logger.info("Closing position: %s %d %s (contractId=%s)", action, qty, symbol, contract_id)
        return True

    # ─────────────────────────────────────────
    # HTTP helpers
    # ─────────────────────────────────────────

    def _get(self, endpoint: str, _retried: bool = False) -> Any:
        self.ensure_token_valid()
        try:
            resp = requests.get(
                f"{self.base_url}{endpoint}", headers=self._headers(), timeout=30
            )
            # Auto re-auth on 401/403 (expired token) — retry once
            if resp.status_code in (401, 403) and not _retried:
                logger.warning("GET %s returned %d. Re-authenticating...", endpoint, resp.status_code)
                if self._re_authenticate():
                    return self._get(endpoint, _retried=True)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("GET %s failed: %s", endpoint, e)
            return None

    def _post(self, endpoint: str, payload: dict, _retried: bool = False) -> Any:
        self.ensure_token_valid()
        try:
            resp = requests.post(
                f"{self.base_url}{endpoint}",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            # Auto re-auth on 401/403 (expired token) — retry once
            if resp.status_code in (401, 403) and not _retried:
                logger.warning("POST %s returned %d. Re-authenticating...", endpoint, resp.status_code)
                if self._re_authenticate():
                    return self._post(endpoint, payload, _retried=True)
            if resp.status_code != 200:
                logger.error(
                    "POST %s status=%d body=%s", endpoint, resp.status_code, resp.text[:500]
                )
            resp.raise_for_status()
            result = resp.json()
            logger.debug("POST %s -> %s", endpoint, result)
            return result
        except requests.RequestException as e:
            logger.error("POST %s failed: %s", endpoint, e)
            return None

    def _re_authenticate(self) -> bool:
        """Clear expired token and do full re-authentication."""
        self.access_token = None
        self.md_access_token = None
        self.token_expiry = None
        return self.authenticate()


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
    # After this many consecutive reconnect failures, signal caller to fall back
    FALLBACK_THRESHOLD = 3

    def __init__(self, md_access_token: str, api: Optional["TradovateAPI"] = None):
        self.md_token = md_access_token
        self._api = api  # Reference to API client for token refresh on 403
        self.ws: Optional[websocket.WebSocketApp] = None
        self._request_id = 0
        self._callbacks: dict[str, list[Callable]] = {}
        self._connected = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._should_run = False
        self._reconnect_count = 0
        self._consecutive_failures = 0
        self.fell_back = threading.Event()  # Signals that WS is unrecoverable
        self._last_data_time: float = 0  # Track when we last received real data

    def start(self):
        """Connect and start listening in a background thread."""
        self._should_run = True
        self._last_data_time = time.time()  # Grace period before staleness check
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
        # Detect proxy for WebSocket connections
        proxy_kwargs = self._get_proxy_kwargs()
        self._thread = threading.Thread(
            target=self.ws.run_forever, kwargs=proxy_kwargs, daemon=True
        )
        self._thread.start()

    @staticmethod
    def _get_proxy_kwargs() -> dict:
        """Extract proxy settings from environment for websocket-client."""
        import re as _re
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        m = _re.match(r"http://([^:]+):([^@]+)@([^:]+):(\d+)", proxy_url)
        if not m:
            return {}
        return {
            "http_proxy_host": m.group(3),
            "http_proxy_port": int(m.group(4)),
            "http_proxy_auth": (m.group(1), m.group(2)),
            "proxy_type": "http",
        }

    # No data for 2 minutes means the connection is stale
    DATA_TIMEOUT = 120

    def stop(self):
        """Close the WebSocket."""
        self._should_run = False
        if self.ws:
            self.ws.close()

    @property
    def data_stale(self) -> bool:
        """True if connected but no data received for DATA_TIMEOUT seconds."""
        if not self._last_data_time:
            return False  # Haven't started receiving yet
        return (time.time() - self._last_data_time) > self.DATA_TIMEOUT

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
                self._last_data_time = time.time()
                self._consecutive_failures = 0  # Real data flowing — connection is healthy
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
        error_str = str(error)
        if "403" in error_str:
            logger.error("Market data WebSocket 403 Forbidden (token expired). Will re-auth on reconnect.")
            self._consecutive_failures += 1
        else:
            logger.error("Market data WebSocket error: %s", error)
            self._consecutive_failures += 1

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning("Market data WebSocket closed: %s %s", close_status_code, close_msg)
        self._connected.clear()
        self._consecutive_failures += 1  # Every close counts as a failure
        # Auto-reconnect (unlimited attempts — bot should never stop trying)
        if self._should_run:
            self._reconnect_count += 1
            # Signal fallback after too many consecutive failures
            if self._consecutive_failures >= self.FALLBACK_THRESHOLD:
                logger.warning(
                    "WebSocket failed %d consecutive times. Signaling fallback to REST polling.",
                    self._consecutive_failures,
                )
                self._should_run = False
                self.fell_back.set()
                return
            # Cap delay at 60 seconds
            delay = min(60, self.RECONNECT_BASE_DELAY * (2 ** (self._reconnect_count - 1)))
            logger.info(
                "Reconnecting in %ds (attempt %d)...",
                delay, self._reconnect_count,
            )
            reconnect_timer = threading.Timer(delay, self._reconnect)
            reconnect_timer.daemon = True
            reconnect_timer.start()

    def _reconnect(self):
        """Reconnect and re-subscribe to all symbols. Refreshes token on 403."""
        # If we have an API reference, refresh the token before reconnecting
        # This fixes the 403 Forbidden issue when the md token expires
        if self._api:
            try:
                self._api.ensure_token_valid()
                if self._api.md_access_token:
                    self.md_token = self._api.md_access_token
                    logger.info("Refreshed market data token for reconnection")
            except Exception as e:
                logger.warning("Token refresh failed during reconnect: %s", e)

        self._connect()
        if self._connected.wait(timeout=15):
            # Don't reset _consecutive_failures here — only reset when
            # real data flows (in _handle_payload). This prevents a cycle
            # of connect→die→reconnect that never reaches fallback threshold.
            for symbol in list(self._callbacks.keys()):
                self._send("md/subscribeQuote", {"symbol": symbol})
                logger.info("Re-subscribed to: %s", symbol)
        elif self._should_run and self._api:
            # Connection failed even with fresh token — try full re-auth
            logger.warning("WebSocket reconnect failed. Attempting full re-authentication...")
            if self._api._re_authenticate() and self._api.md_access_token:
                self.md_token = self._api.md_access_token
                logger.info("Full re-auth succeeded, retrying WebSocket connection...")
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


# ─────────────────────────────────────────────
# REST-based Market Data (fallback when WebSocket is blocked)
# ─────────────────────────────────────────────

# Yahoo Finance symbol mapping for futures front-month
YAHOO_SYMBOLS = {
    "NQ": "NQ=F", "ES": "ES=F", "GC": "GC=F", "CL": "CL=F",
    "SI": "SI=F", "NG": "NG=F",
}
_YAHOO_SYMBOLS = YAHOO_SYMBOLS  # backward compat


class YahooFinanceSession:
    """
    Handles Yahoo Finance API authentication (crumb + cookies).

    Yahoo's v8 chart API requires a valid crumb and session cookies.
    This class fetches them once and reuses them for subsequent requests.
    Falls back to unauthenticated requests if crumb fetch fails.
    """

    _instance: Optional["YahooFinanceSession"] = None

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        })
        self._crumb: Optional[str] = None
        self._initialized = False

    @classmethod
    def get(cls) -> "YahooFinanceSession":
        """Get or create the singleton session."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_crumb(self):
        """Fetch crumb and cookies from Yahoo Finance."""
        if self._initialized:
            return
        self._initialized = True
        try:
            # Step 1: Get cookies by visiting Yahoo Finance consent page
            self._session.get("https://fc.yahoo.com", timeout=10)
            # Step 2: Fetch crumb using the cookies
            resp = self._session.get(
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
                timeout=10,
            )
            if resp.status_code == 200 and resp.text:
                self._crumb = resp.text.strip()
                logger.info("Yahoo Finance crumb acquired")
            else:
                logger.warning(
                    "Yahoo Finance crumb fetch returned %d", resp.status_code
                )
        except Exception as e:
            logger.warning("Yahoo Finance crumb init failed: %s", e)

    def reset(self):
        """Reset session state to force re-authentication on next use."""
        self._crumb = None
        self._initialized = False
        self._session.cookies.clear()

    def fetch_chart(self, yahoo_symbol: str, interval: str = "1m", range_: str = "1d") -> Optional[dict]:
        """
        Fetch chart data for a Yahoo Finance symbol.
        Returns the parsed JSON response or None on failure.
        """
        self._init_crumb()

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
        params = {"interval": interval, "range": range_}
        if self._crumb:
            params["crumb"] = self._crumb

        try:
            resp = self._session.get(url, params=params, timeout=10)

            # If 401/403, reset crumb and retry once
            if resp.status_code in (401, 403) and self._crumb:
                logger.info("Yahoo returned %d, refreshing crumb...", resp.status_code)
                self.reset()
                self._init_crumb()
                if self._crumb:
                    params["crumb"] = self._crumb
                resp = self._session.get(url, params=params, timeout=10)

            if resp.status_code != 200:
                logger.warning(
                    "Yahoo chart request for %s returned %d",
                    yahoo_symbol, resp.status_code,
                )
                return None

            return resp.json()
        except requests.RequestException as e:
            logger.warning("Yahoo chart request failed for %s: %s", yahoo_symbol, e)
            return None


class RestMarketDataPoller:
    """
    Polls Yahoo Finance REST API for market data when WebSocket is unavailable.

    Drop-in replacement for MarketDataStream: same subscribe_quote() /
    on_quote() / start() / stop() interface so bot.py can use either.
    """

    POLL_INTERVAL = 5  # seconds between polls

    def __init__(self, md_access_token: str = ""):
        # md_access_token accepted for interface compatibility but unused
        self._callbacks: dict[str, list[Callable]] = {}
        self._symbols: dict[str, str] = {}  # contract_name -> yahoo symbol
        self._thread: Optional[threading.Thread] = None
        self._should_run = False
        self._last_ts: dict[str, int] = {}  # last processed candle timestamp per symbol
        self._total_candles_dispatched = 0
        self._poll_count = 0

    def start(self):
        """Start polling in a background thread."""
        self._should_run = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("REST market data poller started (interval=%ds)", self.POLL_INTERVAL)

    def stop(self):
        """Stop the polling thread."""
        self._should_run = False

    def subscribe_quote(self, symbol: str, callback: Callable):
        """Register a callback for a symbol. symbol is the contract name (e.g. NQH6)."""
        self._callbacks.setdefault(symbol, []).append(callback)
        # Map contract name to Yahoo symbol (strip month code + year digit)
        # Format: NQH6 -> NQ, ESH6 -> ES, GCG6 -> GC, CLJ6 -> CL
        root = symbol[:-2] if len(symbol) > 2 else symbol
        yahoo_sym = _YAHOO_SYMBOLS.get(root)
        if yahoo_sym:
            self._symbols[symbol] = yahoo_sym
            logger.info("Subscribed to REST quotes: %s -> %s", symbol, yahoo_sym)
        else:
            logger.warning("No Yahoo symbol mapping for %s (root=%s)", symbol, root)

    def unsubscribe_quote(self, symbol: str):
        self._callbacks.pop(symbol, None)
        self._symbols.pop(symbol, None)

    def on_quote(self, symbol: str, callback: Callable):
        self._callbacks.setdefault(symbol, []).append(callback)

    def _poll_loop(self):
        """Periodically fetch quotes and dispatch to callbacks."""
        while self._should_run:
            try:
                self._fetch_and_dispatch()
            except Exception as e:
                logger.error("REST poller error: %s", e)
            self._poll_count += 1
            # Log data health every ~60 seconds (12 polls × 5s)
            if self._poll_count % 12 == 0:
                logger.info(
                    "REST poller health: %d candles dispatched, %d symbols tracked, %d polls",
                    self._total_candles_dispatched, len(self._symbols), self._poll_count,
                )
            time.sleep(self.POLL_INTERVAL)

    def _fetch_and_dispatch(self):
        """Fetch 1-min candles from Yahoo Finance and dispatch new bars to callbacks."""
        if not self._symbols:
            return

        yahoo = YahooFinanceSession.get()

        for contract_name, yahoo_sym in list(self._symbols.items()):
            try:
                data = yahoo.fetch_chart(yahoo_sym)
                if data is None:
                    continue
                result = data.get("chart", {}).get("result", [{}])[0]
                timestamps = result.get("timestamp") or []
                quotes = result.get("indicators", {}).get("quote", [{}])[0]

                highs = quotes.get("high", [])
                lows = quotes.get("low", [])
                closes = quotes.get("close", [])
                volumes = quotes.get("volume", [])

                if not timestamps or not closes:
                    continue

                # Only dispatch candles newer than the last one we processed
                last_ts = self._last_ts.get(contract_name, 0)
                cbs = self._callbacks.get(contract_name, [])
                if not cbs:
                    continue

                for i, ts in enumerate(timestamps):
                    if ts <= last_ts:
                        continue

                    c = closes[i] if i < len(closes) else None
                    h = highs[i] if i < len(highs) else None
                    l = lows[i] if i < len(lows) else None
                    v = volumes[i] if i < len(volumes) else 0

                    if c is None or h is None or l is None:
                        continue

                    quote = {
                        "trade": {"price": c, "size": v or 0},
                        "bid": {"price": c},
                        "high": {"price": h},
                        "low": {"price": l},
                    }

                    self._total_candles_dispatched += 1
                    for cb in cbs:
                        try:
                            cb(contract_name, quote)
                        except Exception as e:
                            logger.error("Quote callback error for %s: %s", contract_name, e)

                self._last_ts[contract_name] = timestamps[-1]

            except Exception as e:
                logger.error("REST poller error for %s: %s", contract_name, e)
