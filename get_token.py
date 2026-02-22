#!/usr/bin/env python3
"""
Get Tradovate Token — ONE-TIME setup script
=============================================
Run this ONCE on your computer. It opens a browser,
you solve the CAPTCHA, and the token is saved forever.
After that, bot.py works automatically.

Usage:
    python get_token.py
"""

import json
import sys
import time
from pathlib import Path

TOKEN_FILE = Path(__file__).parent / ".tradovate_token.json"


def main():
    # Load config
    from dotenv import load_dotenv
    import os
    load_dotenv()

    username = os.getenv("TRADOVATE_USERNAME", "")
    password = os.getenv("TRADOVATE_PASSWORD", "")
    env = os.getenv("TRADOVATE_ENV", "live")
    prop_firm = os.getenv("PROP_FIRM", "")

    # Determine organization
    org_map = {"fundednext": "funded-next"}
    org = os.getenv("TRADOVATE_ORGANIZATION", "") or org_map.get(prop_firm, "")

    url = "https://trader.tradovate.com" if env == "live" else "https://demo.tradovatetrader.com"

    print("=" * 50)
    print("  TRADOVATE TOKEN SETUP (one-time)")
    print("=" * 50)
    print(f"  User: {username}")
    print(f"  Env:  {env}")
    print(f"  Org:  {org or '(none)'}")
    print(f"  URL:  {url}")
    print("=" * 50)
    print()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Installing playwright...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        from playwright.sync_api import sync_playwright

    captured_token = {}

    def on_response(response):
        if captured_token:
            return
        try:
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            data = response.json()
            if isinstance(data, dict) and "accessToken" in data:
                captured_token.update(data)
                print(f"\n*** TOKEN CAPTURED! userId={data.get('userId')} ***\n")
        except Exception:
            pass

    with sync_playwright() as pw:
        print("Opening browser...")
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # Try to select organization
        if org:
            for sel in [
                'a:has-text("institution")',
                'a:has-text("Institution")',
            ]:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    page.wait_for_timeout(1000)
                    break
            for sel in [
                'input[name="organization"]',
                'input[placeholder*="institution" i]',
            ]:
                el = page.query_selector(sel)
                if el:
                    el.fill(org)
                    page.wait_for_timeout(500)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(500)
                    break

        # Fill login form
        if username and password:
            for usel, psel in [
                ('input[name="name"]', 'input[name="password"]'),
                ("input[type='text']", "input[type='password']"),
            ]:
                uf = page.query_selector(usel)
                pf = page.query_selector(psel)
                if uf and pf:
                    uf.fill(username)
                    pf.fill(password)
                    btn = page.query_selector('button[type="submit"]')
                    if btn:
                        btn.click()
                    print("Login submitted. Solve the CAPTCHA in the browser!")
                    break

        # Wait for token
        print("\nWaiting for login... (solve CAPTCHA if it appears)")
        deadline = time.time() + 300  # 5 minutes
        while time.time() < deadline and not captured_token:
            page.wait_for_timeout(1000)

        browser.close()

    if not captured_token:
        print("ERROR: Could not capture token. Try again.")
        sys.exit(1)

    # Save token
    token_data = {
        "accessToken": captured_token["accessToken"],
        "mdAccessToken": captured_token.get("mdAccessToken"),
        "userId": captured_token.get("userId"),
        "accountSpec": captured_token.get("name"),
        "accountId": None,
        "expirationTime": captured_token.get("expirationTime"),
        "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))

    print(f"Token saved to {TOKEN_FILE}")
    print()
    print("DONE! Now run:")
    print("  python bot.py --live")
    print()
    print("The bot will auto-renew the token. No more CAPTCHA needed.")


if __name__ == "__main__":
    main()
