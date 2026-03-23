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
            try:
                data = resp.json()
            except (ValueError, requests.exceptions.JSONDecodeError):
                logger.error("Web auth: non-JSON response (status=%d): %s", resp.status_code, resp.text[:200])
                return None
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
                # Sync md_access_token so WebSocket market data stays authenticated
                if data.get("mdAccessToken"):
                    self.md_access_token = data["mdAccessToken"]
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
        # Renew if less than 15 minutes remain (proactive — avoids expiry during slow API calls)
        if remaining < 900:
            if remaining <= 0:
                logger.warning("Token EXPIRED (%.0fs ago). Attempting full re-auth...", -remaining)
            else:
                logger.info("Token expiring in %.0fs. Renewing...", remaining)
            if not self.renew_token():
                logger.warning("Token renewal failed. Attempting full re-authentication...")
                # Clear expired token so authenticate() doesn't short-circuit
                self.access_token = None
                self.md_access_token = None
                if not self.authenticate():
                    # Do NOT restore expired token — it will cause 401 loops.
                    # Leave token as None so next API call triggers re-auth via 401 handler.
                    logger.error("Full re-authentication also failed! Token is now None.")

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

    def get_order_detail(self, order_id: int) -> Optional[dict]:
        """Get detailed info for a specific order (status, avgPrice, filledQty)."""
        try:
            return self._get(f"/order/item?id={order_id}")
        except Exception as e:
            logger.warning("Failed to get order detail for %s: %s", order_id, e)
            return None

    def get_order_fills(self, order_id: int) -> list[dict]:
        """Get all fills for a specific order."""
        try:
            result = self._get(f"/fill/deps?masterid={order_id}")
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("Failed to get fills for order %s: %s", order_id, e)
            return []

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

    def get_contract_maturity(self, contract_name: str) -> Optional[str]:
        """
        Get the expiration/maturity date for a contract.
        Returns ISO date string (e.g. '2026-03-21') or None.
        """
        contract = self.find_contract(contract_name)
        if contract:
            return contract.get("expirationDate") or contract.get("contractMaturityDate")
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
        if self.account_id is None:
            logger.error("Cannot place bracket order: account_id is None (auth may have failed)")
            return None
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
            "Placing bracket %s %d %s @ %s | SL=%.2f TP=%.2f | payload=%s",
            action, qty, symbol, order_type, stop_price, take_profit_price,
            entry_payload,
        )

        entry_result = self._post("/order/placeorder", entry_payload)
        if not entry_result or "orderId" not in entry_result:
            logger.error("Entry order failed: %s", entry_result)
            return None

        entry_order_id = entry_result["orderId"]
        entry_status = entry_result.get("ordStatus", "Unknown")
        logger.info(
            "Entry order placed: orderId=%s status=%s | full_response=%s",
            entry_order_id, entry_status, entry_result,
        )

        # Check if order was rejected
        if entry_status == "Rejected":
            reject_reason = entry_result.get("rejectReason", entry_result.get("text", "unknown"))
            logger.error("Entry order REJECTED: %s", reject_reason)
            return None

        # For market orders, verify fill after brief delay
        fill_price = 0
        if order_type == "Market":
            time.sleep(1)
            order_detail = self._get(f"/order/item?id={entry_order_id}")
            if order_detail:
                detail_status = order_detail.get("ordStatus", "Unknown")
                filled_qty = order_detail.get("filledQty", 0)
                avg_price = order_detail.get("avgPrice", 0)
                fill_price = avg_price
                logger.info(
                    "Entry order check: orderId=%s status=%s filled=%s avgPrice=%s | detail=%s",
                    entry_order_id, detail_status, filled_qty, avg_price, order_detail,
                )
                if detail_status == "Rejected":
                    logger.error(
                        "Entry order REJECTED after submit: reason=%s text=%s | full=%s",
                        order_detail.get("rejectReason"),
                        order_detail.get("text"),
                        order_detail,
                    )
                    # Try commandReport for more details
                    try:
                        cmd_report = self._get(f"/commandReport/deps?masterid={entry_order_id}")
                        if cmd_report:
                            logger.error("CommandReport for rejected order: %s", cmd_report)
                    except Exception:
                        pass
                    return None

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
            # CRITICAL: Entry exists WITHOUT stop-loss protection.
            # Cancel the unprotected entry to avoid unlimited risk.
            logger.critical(
                "Cancelling unprotected entry order %s — no SL/TP attached!", entry_order_id
            )
            self._post("/order/cancelorder", {"orderId": entry_order_id})
            return None
        else:
            logger.info(
                "OCO placed: SL orderId=%s TP orderId=%s",
                oco_result.get("orderId"), oco_result.get("ocoId"),
            )

        return {
            "orderId": entry_order_id,
            "slOrderId": oco_result.get("orderId") if oco_result else None,
            "tpOrderId": oco_result.get("ocoId") if oco_result else None,
            "fillPrice": fill_price,
        }

    def place_market_order(
        self, symbol: str, action: str, qty: int
    ) -> Optional[dict]:
        """Place a simple market order (no brackets)."""
        if self.account_id is None:
            logger.error("Cannot place market order: account_id is None")
            return None
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

    def modify_order(self, order_id: int, new_stop_price: float) -> Optional[dict]:
        """Modify an existing stop order's price (for breakeven/trailing stop).

        Uses the /order/modifyorder endpoint. Preserves OCO linkage.
        Returns the updated order dict or None on failure.
        """
        payload = {
            "orderId": order_id,
            "stopPrice": new_stop_price,
            "isAutomated": True,
        }
        logger.info("Modifying order %s → new stopPrice=%.4f", order_id, new_stop_price)
        result = self._post("/order/modifyorder", payload)
        if result and "orderId" in result:
            logger.info("Order %s modified successfully: %s", order_id, result)
            return result
        else:
            logger.error("Failed to modify order %s: %s", order_id, result)
            return None

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
            try:
                return resp.json()
            except (ValueError, requests.exceptions.JSONDecodeError):
                logger.error("GET %s: non-JSON response (status=%d): %s", endpoint, resp.status_code, resp.text[:200])
                return None
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
            try:
                result = resp.json()
            except (ValueError, requests.exceptions.JSONDecodeError):
                logger.error("POST %s: non-JSON response (status=%d): %s", endpoint, resp.status_code, resp.text[:200])
                return None
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
    FALLBACK_THRESHOLD = 10

    def __init__(self, md_access_token: str, api: Optional["TradovateAPI"] = None):
        self.md_token = md_access_token
        self._api = api  # Reference to API client for token refresh on 403
        self.ws: Optional[websocket.WebSocketApp] = None
        # Start at 1 to avoid collision with hardcoded auth request_id=1
        self._request_id = 1
        self._callbacks: dict[str, list[Callable]] = {}
        # Maps contractId (int) → symbol name (str) for correct quote routing
        self._contract_id_to_symbol: dict[int, str] = {}
        # Maps request_id → symbol for auto-learning contractId from subscription responses
        self._pending_subscriptions: dict[int, str] = {}
        self._callbacks_lock = threading.Lock()
        self._connected = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._should_run = False
        self._reconnect_count = 0
        self._consecutive_failures = 0
        self.fell_back = threading.Event()  # Signals that WS is unrecoverable
        self._last_data_time: float = 0  # Track when we last received real data
        self._quotes_received: int = 0  # Count of actual market data events received
        self._quotes_dispatched: int = 0  # Count of quotes successfully routed to callbacks
        self._start_time: float = 0  # When this stream was started
        self._reconnect_timer: Optional[threading.Timer] = None
        self._heartbeat_timer: Optional[threading.Timer] = None
        self._got_403: bool = False  # Set by _on_error when token expired

    def start(self):
        """Connect and start listening in a background thread."""
        self._should_run = True
        self._last_data_time = time.time()  # Grace period before staleness check
        self._start_time = time.time()
        self._quotes_received = 0
        self._connect()
        self._connected.wait(timeout=15)

    def _connect(self):
        """Create WebSocket and connect."""
        # Wait for old thread to finish before starting a new connection
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        self.ws = websocket.WebSocketApp(
            config.WS_MARKET_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # Detect proxy for WebSocket connections
        proxy_kwargs = self._get_proxy_kwargs()
        # Send WebSocket-level pings every 25s to keep the TCP connection alive
        # through load balancers, NAT, and server idle timeouts.
        # Without this, the server closes the connection after ~30s of
        # transport-level "silence" (application-level SockJS heartbeats don't count).
        proxy_kwargs["ping_interval"] = 25
        proxy_kwargs["ping_timeout"] = 10
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

    # SockJS application-level heartbeat interval (seconds).
    # Tradovate's server closes idle connections after ~30s of silence at
    # the application layer.  WebSocket-level pings don't count — the server
    # expects SockJS frames.  Sending "[]" (empty SockJS message) every 10s
    # keeps the connection alive reliably.
    _HEARTBEAT_INTERVAL = 10

    def stop(self):
        """Close the WebSocket and cancel any pending reconnect."""
        self._should_run = False
        self._stop_heartbeat()
        # Cancel pending reconnect timer
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        # Wait briefly for the thread to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _start_heartbeat(self):
        """Start sending periodic SockJS heartbeats ("[]") to keep alive."""
        self._stop_heartbeat()

        def _beat():
            if not self._should_run or not self._connected.is_set():
                return
            if self.ws:
                try:
                    self.ws.send("[]")
                except Exception:
                    pass  # Connection lost — _on_close will handle reconnect
            # Schedule next beat
            if self._should_run:
                self._heartbeat_timer = threading.Timer(self._HEARTBEAT_INTERVAL, _beat)
                self._heartbeat_timer.daemon = True
                self._heartbeat_timer.start()

        self._heartbeat_timer = threading.Timer(self._HEARTBEAT_INTERVAL, _beat)
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()

    def _stop_heartbeat(self):
        """Cancel the periodic heartbeat timer."""
        if self._heartbeat_timer:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None

    # How long to wait for first quote before declaring stream dead
    NO_DATA_TIMEOUT = 60  # seconds

    @property
    def data_stale(self) -> bool:
        """True if connected but no data received for DATA_TIMEOUT seconds,
        or if no quotes at all after NO_DATA_TIMEOUT since start."""
        if not self._last_data_time:
            return False  # Haven't started receiving yet
        # Check 1: No data for DATA_TIMEOUT seconds (normal staleness)
        if (time.time() - self._last_data_time) > self.DATA_TIMEOUT:
            return True
        # Check 2: Stream running but zero quotes received after NO_DATA_TIMEOUT
        if self._quotes_received == 0 and self._start_time:
            if (time.time() - self._start_time) > self.NO_DATA_TIMEOUT:
                logger.warning(
                    "WebSocket has been running for %.0fs but received 0 quotes. Declaring stale.",
                    time.time() - self._start_time,
                )
                return True
        return False

    def subscribe_quote(self, symbol: str, callback: Callable, contract_id: int = None):
        """Subscribe to real-time quotes for a symbol."""
        with self._callbacks_lock:
            self._callbacks.setdefault(symbol, []).append(callback)
        if contract_id is not None:
            self._contract_id_to_symbol[contract_id] = symbol
        # Track request_id for auto-learning contractId from response
        next_req_id = self._request_id + 1
        self._pending_subscriptions[next_req_id] = symbol
        self._send("md/subscribeQuote", {"symbol": symbol})
        logger.info("Subscribed to quotes: %s (contractId=%s)", symbol, contract_id)

    def unsubscribe_quote(self, symbol: str):
        """Unsubscribe from quotes."""
        self._send("md/unsubscribeQuote", {"symbol": symbol})
        with self._callbacks_lock:
            self._callbacks.pop(symbol, None)

    def on_quote(self, symbol: str, callback: Callable):
        """Register a callback for quote updates on a symbol."""
        with self._callbacks_lock:
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
                self._start_heartbeat()
                continue

            # Auth failure — trigger reconnect with fresh token
            if item.get("i") == 1 and item.get("s") != 200:
                logger.error("Market data auth failed (status=%s): %s", item.get("s"), item)
                self._got_403 = True  # Force full re-auth on reconnect
                if self.ws:
                    try:
                        self.ws.close()  # Trigger _on_close -> reconnect with fresh token
                    except Exception:
                        pass
                continue

            # Subscription response — auto-learn contractId mapping
            req_id = item.get("i")
            if req_id and req_id in self._pending_subscriptions:
                sym = self._pending_subscriptions.pop(req_id)
                status = item.get("s")
                if status == 200:
                    logger.info("Subscription confirmed for %s (status=200)", sym)
                    # Extract contractId from response body if available
                    body = item.get("d", {})
                    if isinstance(body, dict):
                        cid = body.get("contractId")
                        if cid is not None and cid not in self._contract_id_to_symbol:
                            self._contract_id_to_symbol[cid] = sym
                            logger.info("Auto-mapped contractId %s -> %s from subscription response", cid, sym)
                else:
                    logger.error(
                        "Subscription FAILED for %s (status=%s): %s",
                        sym, status, item,
                    )
                continue

            # Quote data — dispatched by symbol from the "d" field
            if "e" in item and item["e"] == "md" and "d" in item:
                self._last_data_time = time.time()
                self._quotes_received += 1
                self._consecutive_failures = 0  # Real data flowing — connection is healthy
                data = item["d"]

                # Log first 5 raw quote payloads for debugging quote structure
                if self._quotes_received <= 5:
                    logger.info("RAW MD #%d keys=%s data=%s", self._quotes_received,
                                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                                json.dumps(data)[:500])

                quotes = data.get("quotes", [data]) if isinstance(data, dict) else [data]

                # contractId may be at the data level (wrapping the quotes) rather than
                # on individual quote entries. Check both locations.
                data_contract_id = (data.get("contractId") or data.get("id")
                                    ) if isinstance(data, dict) else None

                # Snapshot callbacks under lock to avoid race with subscribe/unsubscribe
                with self._callbacks_lock:
                    cb_snapshot = {sym: list(cbs) for sym, cbs in self._callbacks.items()}
                for quote in quotes:
                    # Try contractId from quote first, fall back to data-level contractId
                    contract_id = (quote.get("contractId") or quote.get("id") or quote.get("cid")
                                   or data_contract_id)
                    # Route quote to the correct symbol's callbacks using contractId.
                    # Without this filter, ALL quotes go to ALL symbols — causing
                    # strategies to receive prices from wrong contracts (e.g., NQ
                    # prices fed to GC strategy), which breaks ORB ranges and VWAP.
                    target_sym = self._contract_id_to_symbol.get(contract_id)
                    if target_sym and target_sym in cb_snapshot:
                        self._quotes_dispatched += 1
                        for cb in cb_snapshot[target_sym]:
                            try:
                                cb(target_sym, quote)
                            except Exception as e:
                                logger.error("Quote callback error for %s: %s", target_sym, e)
                    elif contract_id is not None and not target_sym:
                        # Unknown contractId — try auto-mapping if only one subscription
                        # is waiting, otherwise log warning for diagnostics.
                        if len(cb_snapshot) == 1:
                            # Only one symbol subscribed — safe to assume this is it
                            for sym, cbs in cb_snapshot.items():
                                self._contract_id_to_symbol[contract_id] = sym
                                logger.info("Auto-mapped unmapped contractId %s -> %s (single symbol)", contract_id, sym)
                                for cb in cbs:
                                    try:
                                        cb(sym, quote)
                                    except Exception as e:
                                        logger.error("Quote callback error for %s: %s", sym, e)
                        else:
                            # Log first 10 unmapped IDs as WARNING for diagnostics
                            unmapped_count = getattr(self, "_unmapped_warn_count", 0)
                            if unmapped_count < 10:
                                self._unmapped_warn_count = unmapped_count + 1
                                logger.warning(
                                    "Unmapped contractId %s — skipping. Known IDs: %s. Callbacks: %s",
                                    contract_id,
                                    list(self._contract_id_to_symbol.keys()),
                                    list(cb_snapshot.keys()),
                                )
                    elif contract_id is None:
                        # No contractId anywhere — log warning and skip
                        # (broadcasting to all strategies would corrupt their state)
                        no_cid_count = getattr(self, "_no_cid_warn_count", 0)
                        if no_cid_count < 10:
                            self._no_cid_warn_count = no_cid_count + 1
                            logger.warning(
                                "Quote without contractId (#%d). data_keys=%s quote_keys=%s. Skipping.",
                                no_cid_count + 1,
                                list(data.keys()) if isinstance(data, dict) else "?",
                                list(quote.keys()) if isinstance(quote, dict) else "?",
                            )
                        if len(cb_snapshot) == 1:
                            # Only one symbol — safe to dispatch
                            for sym, cbs in cb_snapshot.items():
                                self._quotes_dispatched += 1
                                for cb in cbs:
                                    try:
                                        cb(sym, quote)
                                    except Exception as e:
                                        logger.error("Quote callback error for %s: %s", sym, e)

    def _on_error(self, ws, error):
        error_str = str(error)
        if "403" in error_str:
            logger.error("Market data WebSocket 403 Forbidden (token expired). Will re-auth on reconnect.")
            self._got_403 = True
        else:
            logger.error("Market data WebSocket error: %s", error)
        # NOTE: Don't increment _consecutive_failures here — _on_close always
        # fires after _on_error and handles the counter.  Double-counting caused
        # premature fallback to REST polling.

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected.clear()
        self._stop_heartbeat()
        # Graceful close (code 1000 "Bye") is normal server behavior — reconnect
        # quickly without counting it as a failure.
        is_graceful = close_status_code == 1000
        if is_graceful:
            logger.info("Market data WebSocket closed gracefully (1000 %s). Reconnecting...", close_msg)
        else:
            logger.warning("Market data WebSocket closed: %s %s", close_status_code, close_msg)
            self._consecutive_failures += 1

        if not self._should_run:
            return

        # Signal fallback after too many consecutive *real* failures
        if self._consecutive_failures >= self.FALLBACK_THRESHOLD:
            logger.warning(
                "WebSocket failed %d consecutive times. Signaling fallback to REST polling.",
                self._consecutive_failures,
            )
            self._should_run = False
            self.fell_back.set()
            return

        self._reconnect_count += 1
        # Cancel any pending reconnect timer to prevent timer leaks
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        # Graceful close: reconnect quickly (1s). Error: exponential backoff up to 60s.
        if is_graceful:
            delay = 1
        else:
            delay = min(60, self.RECONNECT_BASE_DELAY * (2 ** (self._reconnect_count - 1)))
        logger.info(
            "Reconnecting in %ds (attempt %d, consecutive failures: %d)...",
            delay, self._reconnect_count, self._consecutive_failures,
        )
        self._reconnect_timer = threading.Timer(delay, self._reconnect)
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()

    def _reconnect(self):
        """Reconnect and re-subscribe to all symbols. Refreshes token on 403."""
        # Close old WebSocket connection to prevent leaking
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

        # If we have an API reference, refresh the token before reconnecting.
        # On 403 (expired token), do a full re-auth instead of just renewal
        # to guarantee we get a fresh md_access_token.
        if self._api:
            try:
                if self._got_403:
                    logger.info("Token was expired (403). Performing full re-authentication...")
                    self._got_403 = False
                    if self._api._re_authenticate() and self._api.md_access_token:
                        self.md_token = self._api.md_access_token
                        logger.info("Full re-auth succeeded, got fresh md token for reconnection")
                    else:
                        logger.warning("Full re-auth failed after 403")
                else:
                    self._api.ensure_token_valid()
                    if self._api.md_access_token:
                        self.md_token = self._api.md_access_token
                        logger.info("Refreshed market data token for reconnection")
            except Exception as e:
                logger.warning("Token refresh failed during reconnect: %s", e)

        self._connect()
        if self._connected.wait(timeout=15):
            # Connection succeeded — reset consecutive failures counter so we
            # don't accumulate stale failure counts from previous disconnect cycles.
            self._consecutive_failures = 0
            with self._callbacks_lock:
                symbols = list(self._callbacks.keys())
            for symbol in symbols:
                # Track re-subscription for auto-learning contractId
                next_req_id = self._request_id + 1
                self._pending_subscriptions[next_req_id] = symbol
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
                    self._consecutive_failures = 0
                    with self._callbacks_lock:
                        symbols = list(self._callbacks.keys())
                    for symbol in symbols:
                        next_req_id = self._request_id + 1
                        self._pending_subscriptions[next_req_id] = symbol
                        self._send("md/subscribeQuote", {"symbol": symbol})
                        logger.info("Re-subscribed to: %s", symbol)

    def _send(self, endpoint: str, body: dict):
        """Send a message using the Tradovate WebSocket protocol."""
        self._request_id += 1
        msg = f"{endpoint}\n{self._request_id}\n\n{json.dumps(body)}"
        if self.ws:
            try:
                self.ws.send(msg)
            except Exception as e:
                logger.warning("WebSocket send failed for %s: %s", endpoint, e)


# ─────────────────────────────────────────────
# REST-based Market Data (fallback when WebSocket is blocked)
# ─────────────────────────────────────────────

# Yahoo Finance symbol mapping for futures front-month
YAHOO_SYMBOLS = {
    # Micro contracts use the same Yahoo symbol as their mini counterparts
    # (Yahoo tracks the same underlying price, micros just have smaller multiplier)
    "MNQ": "NQ=F", "MES": "ES=F", "MGC": "GC=F", "MCL": "CL=F",
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

    def subscribe_quote(self, symbol: str, callback: Callable, contract_id: int = None):
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
                    v = (volumes[i] or 0) if i < len(volumes) else 0

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
