"""
market.py — Live market data via KiteTicker WebSocket
Streams real-time LTP, OI, volume for Nifty spot + option chain
"""
import asyncio, logging, datetime
from kiteconnect import KiteTicker

log = logging.getLogger("GEKKO.market")

# Nifty spot instrument token (NSE)
NIFTY_SPOT_TOKEN = 256265
INDIA_VIX_TOKEN  = 264969

class MarketFeed:
    def __init__(self, state):
        self.state = state
        self.ticker: KiteTicker | None = None
        self._running = False
        self._subscribed_tokens: set[int] = set()

    async def ticker_loop(self):
        """Reconnects KiteTicker whenever auth is ready"""
        while True:
            await asyncio.sleep(5)
            if not self.state.kite or self.state.auth_status != "connected":
                continue
            if self._running:
                continue
            try:
                await self._start_ticker()
            except Exception as e:
                log.error(f"Ticker start error: {e}")
                self._running = False
                await asyncio.sleep(10)

    async def _start_ticker(self):
        """Start KiteTicker in a thread (it's synchronous internally)"""
        import threading
        kite = self.state.kite
        api_key = kite.api_key
        access_token = kite.access_token

        self.ticker = KiteTicker(api_key, access_token)

        def on_ticks(ws, ticks):
            for tick in ticks:
                self._process_tick(tick)

        def on_connect(ws, response):
            log.info("KiteTicker connected")
            self._running = True
            # Always subscribe to Nifty spot + VIX
            tokens = [NIFTY_SPOT_TOKEN, INDIA_VIX_TOKEN]
            # Add option chain tokens if we have them
            tokens += list(self._subscribed_tokens)
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        def on_close(ws, code, reason):
            log.warning(f"KiteTicker closed: {code} {reason}")
            self._running = False

        def on_error(ws, code, reason):
            log.error(f"KiteTicker error: {code} {reason}")
            self._running = False

        def on_reconnect(ws, attempts_count):
            log.info(f"KiteTicker reconnecting... attempt {attempts_count}")

        self.ticker.on_ticks     = on_ticks
        self.ticker.on_connect   = on_connect
        self.ticker.on_close     = on_close
        self.ticker.on_error     = on_error
        self.ticker.on_reconnect = on_reconnect

        # Run ticker in background thread (it blocks)
        thread = threading.Thread(target=self.ticker.connect, kwargs={"threaded": True})
        thread.daemon = True
        thread.start()
        log.info("KiteTicker thread started")

    def _process_tick(self, tick: dict):
        """Handle incoming tick data — update state"""
        token = tick["instrument_token"]

        if token == NIFTY_SPOT_TOKEN:
            self.state.spot = tick.get("last_price", self.state.spot)
            self.state.spot_ohlc = tick.get("ohlc", {})

        elif token == INDIA_VIX_TOKEN:
            self.state.vix = tick.get("last_price", self.state.vix)
            self._update_iv_rank()

        else:
            # Option tick — update chain
            self._update_option_tick(token, tick)

    def _update_iv_rank(self):
        """Approximate IV Rank from current VIX vs 52-week range"""
        vix_52w_low  = float(self.state.config.get("vix_52w_low", 10.0))
        vix_52w_high = float(self.state.config.get("vix_52w_high", 35.0))
        self.state.iv_rank = round(
            (self.state.vix - vix_52w_low) / (vix_52w_high - vix_52w_low) * 100, 1
        )

    def _update_option_tick(self, token: int, tick: dict):
        """Update an option in the chain by its instrument token"""
        for opt in self.state.option_chain:
            if opt.get("token") == token:
                old_ltp = opt.get("ltp", 0)
                opt["ltp"] = tick.get("last_price", old_ltp)
                opt["oi"]  = tick.get("oi", opt.get("oi", 0))
                opt["volume"] = tick.get("volume", opt.get("volume", 0))
                opt["bid"] = tick.get("depth", {}).get("buy", [{}])[0].get("price", 0)
                opt["ask"] = tick.get("depth", {}).get("sell", [{}])[0].get("price", 0)
                # Recalculate Greeks on each tick
                self._recalc_greeks(opt)
                break

        # Also update open position LTP
        for pos in self.state.positions:
            if pos.get("token") == token:
                pos["ltp"] = tick.get("last_price", pos.get("ltp", 0))
                pos["pnl"] = self._calc_position_pnl(pos)

        # Recalc session PnL
        self.state.session_pnl = sum(p.get("pnl", 0) for p in self.state.positions)

    def _calc_position_pnl(self, pos: dict) -> float:
        qty    = pos.get("qty", 0)
        entry  = pos.get("entry", 0)
        ltp    = pos.get("ltp", entry)
        side   = pos.get("side", "buy")  # buy or sell
        multiplier = 1 if side == "buy" else -1
        return round((ltp - entry) * qty * multiplier, 2)

    def _recalc_greeks(self, opt: dict):
        """Recalculate IV and Greeks on each price tick"""
        try:
            from greeks import BSGreeks
            S = self.state.spot
            K = opt["strike"]
            T = self._time_to_expiry(opt.get("expiry", ""))
            r = 0.065
            ltp = opt["ltp"]
            if ltp <= 0 or T <= 0:
                return
            g = BSGreeks.full_greeks(S, K, T, r, ltp, opt["type"])
            opt.update(g)
            # IV mismatch vs fair (VIX-based)
            fair_iv = self.state.vix
            opt["iv_mismatch"] = round(opt["iv"] - fair_iv, 2)
            opt["overpriced"]  = opt["iv_mismatch"] >= self.state.config.get("iv_mismatch_threshold", 2.0)
        except Exception:
            pass

    def _time_to_expiry(self, expiry_str: str) -> float:
        try:
            exp = datetime.datetime.strptime(expiry_str, "%d%b%y")
            now = datetime.datetime.now()
            return max((exp - now).total_seconds() / (365.25 * 24 * 3600), 1/365)
        except Exception:
            return 7/365  # default 1 week

    def subscribe_option_tokens(self, tokens: list[int]):
        """Subscribe to additional option tokens on the fly"""
        new_tokens = [t for t in tokens if t not in self._subscribed_tokens]
        if new_tokens and self.ticker and self._running:
            self.ticker.subscribe(new_tokens)
            self.ticker.set_mode(self.ticker.MODE_FULL, new_tokens)
            self._subscribed_tokens.update(new_tokens)
            log.info(f"Subscribed to {len(new_tokens)} new option tokens")

    async def stop(self):
        if self.ticker:
            self.ticker.close()
        self._running = False
