"""
auth.py — Automated Zerodha login using Playwright headless browser
Runs daily at 08:30 IST, refreshes access_token automatically
"""
import os, json, asyncio, logging, datetime
from pathlib import Path
from kiteconnect import KiteConnect

log = logging.getLogger("GEKKO.auth")

CREDENTIALS = {
    "api_key":     os.getenv("ZERODHA_API_KEY", ""),
    "api_secret":  os.getenv("ZERODHA_API_SECRET", ""),
    "user_id":     os.getenv("ZERODHA_USER_ID", ""),
    "password":    os.getenv("ZERODHA_PASSWORD", ""),
    "totp_secret": os.getenv("ZERODHA_TOTP_SECRET", ""),  # for 2FA
}

TOKEN_FILE = Path("/tmp/gekko_token.json")

class ZerodhaAuth:
    def __init__(self, state):
        self.state = state
        self.kite = None

    def get_login_url(self) -> str:
        """Manual fallback: returns Zerodha login URL"""
        kite = KiteConnect(api_key=CREDENTIALS["api_key"])
        return kite.login_url()

    async def exchange_token(self, request_token: str):
        """Manual fallback: exchange request_token for access_token"""
        kite = KiteConnect(api_key=CREDENTIALS["api_key"])
        data = kite.generate_session(request_token, api_secret=CREDENTIALS["api_secret"])
        self._save_and_apply(kite, data["access_token"])
        log.info("Manual token exchange successful")

    def _save_and_apply(self, kite: KiteConnect, access_token: str):
        """Save token to file and apply to state"""
        kite.set_access_token(access_token)
        self.state.kite = kite
        self.state.auth_status = "connected"
        self.state.token_expires = (datetime.datetime.now() + datetime.timedelta(hours=16)).isoformat()
        # Save for restarts
        TOKEN_FILE.write_text(json.dumps({
            "access_token": access_token,
            "date": datetime.date.today().isoformat()
        }))
        log.info(f"Zerodha connected. Token saved.")

    def _try_load_saved_token(self) -> bool:
        """On startup, try to reuse today's token if already fetched"""
        if not TOKEN_FILE.exists():
            return False
        try:
            data = json.loads(TOKEN_FILE.read_text())
            if data["date"] != datetime.date.today().isoformat():
                return False
            kite = KiteConnect(api_key=CREDENTIALS["api_key"])
            self._save_and_apply(kite, data["access_token"])
            log.info("Reused saved token from today")
            return True
        except Exception as e:
            log.warning(f"Could not reuse saved token: {e}")
            return False

    async def auto_login_loop(self):
        """
        Main loop: 
        1. On startup, try to reuse today's saved token
        2. Every day at 08:30 IST, run headless browser login
        3. Retry on failure with backoff
        """
        # Try reusing today's token first (fast restart)
        if self._try_load_saved_token():
            self.state.add_log("GEKKO", "Token loaded from cache. Zerodha connected ✓", "info")
        else:
            # First run: attempt login immediately
            await self._headless_login()

        # Schedule daily refresh at 08:30 IST
        while True:
            now = datetime.datetime.now()
            # Calculate seconds until next 08:30
            target = now.replace(hour=8, minute=30, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            sleep_secs = (target - now).total_seconds()
            log.info(f"Next auto-login in {sleep_secs/3600:.1f} hours")
            await asyncio.sleep(sleep_secs)
            await self._headless_login()

    async def _headless_login(self, retry: int = 3):
        """
        Uses Playwright to automate Zerodha Kite login:
        1. Navigate to login page
        2. Fill user_id + password
        3. Extract and submit TOTP (2FA)
        4. Capture request_token from redirect URL
        5. Exchange for access_token
        """
        for attempt in range(1, retry + 1):
            try:
                log.info(f"Auto-login attempt {attempt}/{retry}")
                self.state.add_log("GEKKO", f"Auto-login starting (attempt {attempt})...", "info")
                await self._run_playwright()
                self.state.add_log("GEKKO", "Zerodha auto-login successful ✓", "info")
                return
            except Exception as e:
                log.error(f"Auto-login attempt {attempt} failed: {e}")
                self.state.add_log("GEKKO", f"Login attempt {attempt} failed: {e}", "alert")
                if attempt < retry:
                    await asyncio.sleep(30 * attempt)  # backoff: 30s, 60s

        self.state.add_log("GEKKO",
            "Auto-login failed after 3 attempts. Visit /login to authenticate manually.", "alert")
        self.state.auth_status = "failed"

    async def _run_playwright(self):
        """Core headless browser automation"""
        import pyotp
        from playwright.async_api import async_playwright

        api_key    = CREDENTIALS["api_key"]
        api_secret = CREDENTIALS["api_secret"]
        user_id    = CREDENTIALS["user_id"]
        password   = CREDENTIALS["password"]
        totp_secret = CREDENTIALS["totp_secret"]

        if not all([api_key, api_secret, user_id, password, totp_secret]):
            raise ValueError("Missing Zerodha credentials in environment variables")

        kite = KiteConnect(api_key=api_key)
        login_url = kite.login_url()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
            )
            page = await context.new_page()

            # ── Step 1: Navigate to login ──
            log.info(f"Navigating to Zerodha login...")
            await page.goto(login_url, wait_until="networkidle")

            # ── Step 2: Fill credentials ──
            await page.wait_for_selector('input[type="text"]', timeout=15000)
            await page.fill('input[type="text"]', user_id)
            await page.fill('input[type="password"]', password)
            await page.click('button[type="submit"]')
            log.info("Credentials submitted")

            # ── Step 3: TOTP (2FA) ──
            await page.wait_for_selector('input[label="External TOTP"]', timeout=15000)
            totp = pyotp.TOTP(totp_secret).now()
            log.info(f"Generated TOTP: {totp}")
            await page.fill('input[label="External TOTP"]', totp)
            await page.click('button[type="submit"]')

            # ── Step 4: Wait for redirect and capture request_token ──
            # Zerodha redirects to: https://your-redirect-url?request_token=XXX&status=success
            redirect_url = os.getenv("ZERODHA_REDIRECT_URL", "https://127.0.0.1")

            async def capture_token(response):
                url = response.url
                if "request_token=" in url:
                    from urllib.parse import urlparse, parse_qs
                    params = parse_qs(urlparse(url).query)
                    tokens = params.get("request_token", [])
                    if tokens:
                        self._pending_request_token = tokens[0]
                        log.info(f"Captured request_token: {tokens[0][:10]}...")

            self._pending_request_token = None
            page.on("response", lambda r: asyncio.ensure_future(capture_token(r)))

            # Wait up to 20 seconds for redirect
            for _ in range(20):
                await asyncio.sleep(1)
                if self._pending_request_token:
                    break

            await browser.close()

            if not self._pending_request_token:
                # Try extracting from current URL as fallback
                current_url = page.url
                if "request_token=" in current_url:
                    from urllib.parse import urlparse, parse_qs
                    params = parse_qs(urlparse(current_url).query)
                    self._pending_request_token = params.get("request_token", [None])[0]

            if not self._pending_request_token:
                raise RuntimeError("Could not capture request_token from Zerodha redirect")

            # ── Step 5: Exchange for access_token ──
            data = kite.generate_session(
                self._pending_request_token,
                api_secret=api_secret
            )
            self._save_and_apply(kite, data["access_token"])
            log.info("Access token obtained and saved.")
