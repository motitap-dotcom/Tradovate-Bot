#!/usr/bin/env python3
"""
Tradovate Browser Bot
=====================
Automated trading bot that authenticates via browser automation.
No API credentials (CID/Secret) required — logs in through the
Tradovate web platform and captures auth tokens from network traffic.

Once tokens are captured, uses the standard Tradovate REST API and
WebSocket for market data and order execution (identical to bot.py).

The browser stays open so you can monitor the Tradovate web trader
while the bot operates via API in the background.

Usage:
    python browser_bot.py                  # Open browser, login, trade
    python browser_bot.py --headless       # Invisible browser
    python browser_bot.py --dry-run        # Signals only, no orders
    python browser_bot.py --no-keep-open   # Close browser after login
"""

import argparse
import logging
import signal
import sys
import time
import threading
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Response, Browser

import config
from bot import TradovateBot

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("browser_bot.log"),
    ],
)
logger = logging.getLogger("browser_bot")

# ─────────────────────────────────────────────
# Tradovate Web Trader URLs
# ─────────────────────────────────────────────

TRADOVATE_WEB_URLS = {
    "demo": "https://demo.tradovatetrader.com",
    "live": "https://trader.tradovate.com",
}


# ─────────────────────────────────────────────
# Browser Token Harvester
# ─────────────────────────────────────────────


class BrowserTokenHarvester:
    """
    Opens Tradovate in a real browser, logs in (auto or manual),
    and captures auth tokens by intercepting network responses.

    Once tokens are captured they can be injected into TradovateAPI
    via set_token(), bypassing the need for CID/Secret entirely.
    """

    def __init__(self, headless: bool = False, keep_open: bool = True):
        self.headless = headless
        self.keep_open = keep_open

        # Captured tokens
        self.access_token: Optional[str] = None
        self.md_access_token: Optional[str] = None
        self.expiration_time: Optional[str] = None
        self.user_id: Optional[int] = None

        # Browser references (kept alive if keep_open)
        self._playwright_ctx = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None

    def harvest(self) -> dict:
        """
        Launch browser, login, capture tokens.
        Returns dict: {accessToken, mdAccessToken, userId, expirationTime}
        Raises RuntimeError if login fails.
        """
        logger.info("=" * 55)
        logger.info("  BROWSER TOKEN HARVESTER")
        logger.info("=" * 55)

        env = config.ENVIRONMENT
        url = TRADOVATE_WEB_URLS.get(env, TRADOVATE_WEB_URLS["demo"])

        pw = sync_playwright().start()
        self._playwright_ctx = pw

        logger.info("Launching Chromium (headless=%s)...", self.headless)
        browser = pw.chromium.launch(
            headless=self.headless,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._browser = browser

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        self._page = page

        # ── Intercept ALL responses for token capture ──
        page.on("response", self._on_response)

        # ── Navigate ──
        logger.info("Navigating to %s ...", url)
        try:
            page.goto(url, wait_until="networkidle", timeout=60_000)
        except Exception as e:
            logger.warning("Navigation timeout (continuing): %s", e)

        # ── Auto-login ──
        self._auto_login(page)

        # ── Wait for token ──
        logger.info("Waiting for auth token (up to 2 minutes)...")
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.access_token:
                break
            page.wait_for_timeout(1_000)

        if not self.access_token and not self.headless:
            logger.info(
                "Token not yet captured. Please log in manually in the browser."
            )
            logger.info("Waiting up to 5 more minutes...")
            deadline = time.time() + 300
            while time.time() < deadline:
                if self.access_token:
                    break
                page.wait_for_timeout(2_000)

        if not self.access_token:
            self.close()
            raise RuntimeError(
                "Could not capture auth token. "
                "Make sure your username/password are correct."
            )

        logger.info("Token captured successfully!")
        logger.info(
            "  userId=%s | expires=%s", self.user_id, self.expiration_time
        )
        logger.info(
            "  mdToken=%s",
            "captured" if self.md_access_token else "not available",
        )

        # Close browser unless user wants to keep it open
        if not self.keep_open:
            self.close()
        else:
            logger.info(
                "Browser stays open for monitoring. "
                "It will close when the bot stops."
            )

        return {
            "accessToken": self.access_token,
            "mdAccessToken": self.md_access_token,
            "userId": self.user_id,
            "expirationTime": self.expiration_time,
        }

    def close(self):
        """Close the browser and Playwright context."""
        try:
            if self._browser:
                self._browser.close()
                self._browser = None
            if self._playwright_ctx:
                self._playwright_ctx.stop()
                self._playwright_ctx = None
        except Exception:
            pass

    # ─── Network Interception ───────────────────

    def _on_response(self, response: Response):
        """Capture auth tokens from any JSON response."""
        if self.access_token:
            return  # Already captured

        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return

            data = response.json()
            if not isinstance(data, dict):
                return

            if "accessToken" in data:
                self.access_token = data["accessToken"]
                self.md_access_token = data.get("mdAccessToken")
                self.user_id = data.get("userId")
                self.expiration_time = data.get("expirationTime")
                logger.info(
                    "Intercepted auth response from: %s", response.url
                )

        except Exception:
            pass  # Silently skip non-JSON or parse errors

    # ─── Auto-Login ─────────────────────────────

    def _auto_login(self, page: Page):
        """Try to auto-fill and submit the login form."""
        username = config.TRADOVATE_USERNAME
        password = config.TRADOVATE_PASSWORD

        if not username or not password:
            logger.info(
                "No credentials in .env file. Please log in manually."
            )
            return

        logger.info("Attempting auto-login for: %s", username)

        # Multiple selector strategies for the login form.
        # Tradovate's web UI may use different selectors depending on version.
        selector_strategies = [
            {
                "user": 'input[name="name"]',
                "pass": 'input[name="password"]',
                "btn": 'button[type="submit"]',
            },
            {
                "user": 'input[name="username"]',
                "pass": 'input[name="password"]',
                "btn": 'button[type="submit"]',
            },
            {
                "user": 'input[placeholder*="user" i]',
                "pass": 'input[placeholder*="pass" i]',
                "btn": 'button[type="submit"]',
            },
            {
                "user": "input[type='text']",
                "pass": "input[type='password']",
                "btn": 'button[type="submit"]',
            },
        ]

        for strategy in selector_strategies:
            try:
                # Wait briefly for the fields to render
                user_field = page.query_selector(strategy["user"])
                pass_field = page.query_selector(strategy["pass"])

                if not user_field or not pass_field:
                    continue

                logger.info("Found login form with: %s", strategy["user"])

                # Fill credentials
                user_field.click()
                user_field.fill(username)

                pass_field.click()
                pass_field.fill(password)

                # Submit
                btn = page.query_selector(strategy["btn"])
                if btn:
                    btn.click()
                else:
                    # Try text-based button selectors
                    for text in ["Log In", "Login", "Sign In", "Submit"]:
                        btn = page.query_selector(
                            f'button:has-text("{text}")'
                        )
                        if btn:
                            btn.click()
                            break
                    else:
                        page.keyboard.press("Enter")

                logger.info("Login form submitted. Waiting for auth...")
                page.wait_for_timeout(5_000)
                return

            except Exception as e:
                logger.debug("Selector strategy failed: %s", e)
                continue

        logger.warning(
            "Could not find login form automatically. "
            "Please log in manually in the browser window."
        )


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Tradovate Browser Trading Bot"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live mode (default: demo)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Paper mode — signals only, no real orders",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser invisibly (no GUI window)",
    )
    parser.add_argument(
        "--no-keep-open",
        action="store_true",
        help="Close browser after login (default: keep open for monitoring)",
    )
    args = parser.parse_args()

    # Apply live mode
    if args.live:
        config.ENVIRONMENT = "live"
        config.REST_URL = config._URLS["live"]["rest"]
        config.WS_TRADING_URL = config._URLS["live"]["ws_trading"]
        config.WS_MARKET_URL = config._URLS["live"]["ws_market"]

    harvester = None

    if not args.dry_run:
        # ── Step 1: Harvest tokens via browser ──
        harvester = BrowserTokenHarvester(
            headless=args.headless,
            keep_open=not args.no_keep_open,
        )
        try:
            tokens = harvester.harvest()
        except RuntimeError as e:
            logger.error("Login failed: %s", e)
            sys.exit(1)

        # ── Step 2: Create bot and inject tokens ──
        bot = TradovateBot(dry_run=False)
        bot.api.set_token(
            access_token=tokens["accessToken"],
            md_access_token=tokens.get("mdAccessToken"),
            user_id=tokens.get("userId"),
            expiration_time=tokens.get("expirationTime"),
        )
    else:
        bot = TradovateBot(dry_run=True)

    # ── Graceful shutdown ──
    def handle_signal(signum, frame):
        logger.info("Signal %s received. Stopping...", signum)
        bot.running = False
        if harvester:
            harvester.close()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # ── Step 3: Start trading ──
    # bot.start() will call api.authenticate() which detects the
    # pre-injected token and skips CID/Secret authentication.
    try:
        bot.start()
    finally:
        if harvester:
            harvester.close()


if __name__ == "__main__":
    main()
