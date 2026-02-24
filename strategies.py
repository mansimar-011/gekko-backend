"""
strategies.py — Strategy A (IV Credit Spread) + Strategy B (Iron Condor)
Called every second by the monitor loop in main.py
"""
import asyncio, logging, datetime
from greeks import BSGreeks

log = logging.getLogger("GEKKO.strategy")

class OptionScanner:
    """Scans live option chain and returns sorted mismatch candidates"""

    def __init__(self, state):
        self.state = state

    def get_expiry(self) -> str:
        """Nearest Thursday expiry"""
        today = datetime.date.today()
        days  = (3 - today.weekday()) % 7
        if days == 0:
            days = 7
        exp   = today + datetime.timedelta(days=days)
        return exp.strftime("%d%b%y").upper()

    def time_to_expiry(self, expiry_str: str) -> float:
        try:
            exp = datetime.datetime.strptime(expiry_str, "%d%b%y")
            now = datetime.datetime.now()
            return max((exp - now).total_seconds() / (365.25 * 24 * 3600), 1/365)
        except Exception:
            return 7/365

    def fetch_and_update_chain(self) -> list:
        """
        Pull fresh option chain from Zerodha quote API.
        Updates state.option_chain with live Greeks.
        Returns list sorted by IV mismatch (most overpriced first).
        """
        kite    = self.state.kite
        spot    = self.state.spot
        vix     = self.state.vix
        expiry  = self.get_expiry()
        T       = self.time_to_expiry(expiry)
        r       = 0.065
        step    = 50
        atm     = round(spot / step) * step
        strikes = [atm + i * step for i in range(-8, 9)]  # 16 strikes around ATM

        threshold = self.state.config.get("iv_mismatch_threshold", 2.0)
        chain     = []

        for strike in strikes:
            for opt_type in ("CE", "PE"):
                symbol = f"NIFTY{expiry}{strike}{opt_type}"
                try:
                    q   = kite.quote([f"NFO:{symbol}"])[f"NFO:{symbol}"]
                    ltp = q.get("last_price", 0)
                    oi  = q.get("oi", 0)
                    if ltp < 3:
                        continue

                    greeks = BSGreeks.full_greeks(spot, strike, T, r, ltp, opt_type)
                    iv_mismatch = round(greeks["iv"] - vix, 2)

                    inst = kite.instruments("NFO")
                    token = next(
                        (i["instrument_token"] for i in inst if i["tradingsymbol"] == symbol),
                        None
                    )

                    chain.append({
                        "symbol":      symbol,
                        "token":       token,
                        "strike":      strike,
                        "type":        opt_type,
                        "expiry":      expiry,
                        "ltp":         ltp,
                        "oi":          oi,
                        "iv_mismatch": iv_mismatch,
                        "overpriced":  iv_mismatch >= threshold,
                        **greeks
                    })
                except Exception:
                    pass

        # Sort: most overpriced first
        chain.sort(key=lambda x: x["iv_mismatch"], reverse=True)
        self.state.option_chain = chain
        return chain

    def hedge_pts(self) -> int:
        now = datetime.datetime.now().hour
        return (self.state.config["pre_noon_hedge_pts"]
                if now < 12 else
                self.state.config["post_noon_hedge_pts"])


# ─────────────────────────────────────────────────────────────
# STRATEGY A — IV CREDIT SPREAD
# ─────────────────────────────────────────────────────────────
class StrategyA:
    """
    Entry:  Find most overpriced option (IV mismatch >= threshold)
            SELL it, BUY a hedge further OTM (400pt pre-noon / 300pt post-noon)
    Exit:   50% premium decay | target hit | SL hit | max rolls
    Roll:   If short leg delta > 0.45, roll up/down by 50pts
    """
    def __init__(self, state, order_mgr):
        self.state     = state
        self.order_mgr = order_mgr
        self.scanner   = OptionScanner(state)
        self.entered   = False
        self.short_leg = None   # the sold option info
        self.hedge_leg = None
        self._scan_counter = 0

    async def tick(self):
        """Called every second by monitor loop"""
        # Refresh chain every 60 seconds
        self._scan_counter += 1
        if self._scan_counter % 60 == 0:
            chain = await asyncio.get_event_loop().run_in_executor(
                None, self.scanner.fetch_and_update_chain)

        # Check exits first
        if self.state.target_hit():
            log.info("[A] Target hit. Closing.")
            self.state.add_log("GEKKO",
                f"TARGET HIT ✓ | PnL: +₹{self.state.session_pnl:.0f} | Closing all.", "trade")
            await self.order_mgr.close_all_positions()
            self.state.active_strategy = None
            self._reset()
            return

        if self.state.sl_hit():
            log.info("[A] SL hit. Closing.")
            self.state.add_log("GEKKO",
                f"STOP LOSS ✗ | PnL: ₹{self.state.session_pnl:.0f} | Closing all.", "alert")
            await self.order_mgr.close_all_positions()
            self.state.active_strategy = None
            self._reset()
            return

        # Enter if no position
        if not self.entered and len(self.state.positions) == 0:
            chain = self.state.option_chain
            if not chain:
                chain = await asyncio.get_event_loop().run_in_executor(
                    None, self.scanner.fetch_and_update_chain)
            await self._enter(chain)
            return

        # Monitor for 50% decay or delta breach
        if self.entered and self.short_leg:
            await self._monitor_positions()

    async def _enter(self, chain: list):
        threshold = self.state.config["iv_mismatch_threshold"]
        # Get most overpriced with decent OI
        candidates = [
            o for o in chain
            if o["iv_mismatch"] >= threshold and o["oi"] > 500_000
        ]
        if not candidates:
            return

        sell_opt  = candidates[0]
        hedge_pts = self.scanner.hedge_pts()
        hedge_strike = (sell_opt["strike"] + hedge_pts
                        if sell_opt["type"] == "CE"
                        else sell_opt["strike"] - hedge_pts)
        expiry    = sell_opt["expiry"]
        hedge_sym = f"NIFTY{expiry}{hedge_strike}{sell_opt['type']}"
        lot_size  = self.state.config["lot_size"]

        self.state.add_log("GEKKO",
            f"IV MISMATCH ▲ {sell_opt['symbol']} | IV: {sell_opt['iv']}% vs fair {self.state.vix:.1f}% "
            f"(+{sell_opt['iv_mismatch']}σ) | Δ {sell_opt['delta']}", "alert")

        # Get hedge LTP
        try:
            hedge_q   = self.state.kite.quote([f"NFO:{hedge_sym}"])[f"NFO:{hedge_sym}"]
            hedge_ltp = hedge_q.get("last_price", 0)
        except Exception:
            hedge_ltp = 5.0

        net_credit = round(sell_opt["ltp"] - hedge_ltp, 2)
        self.state.add_log("GEKKO",
            f"ENTERING: SELL {sell_opt['symbol']} @ {sell_opt['ltp']} | "
            f"BUY {hedge_sym} @ {hedge_ltp} | Net Credit: {net_credit}pts", "trade")

        # Place orders
        sell_pos = await self.order_mgr.place_order(
            symbol=sell_opt["symbol"], side="sell",
            qty=lot_size, price=sell_opt["ltp"],
            token=sell_opt.get("token"), tag="A_SHORT"
        )
        hedge_pos = await self.order_mgr.place_order(
            symbol=hedge_sym, side="buy",
            qty=lot_size, price=hedge_ltp,
            tag="A_HEDGE"
        )

        if sell_pos and hedge_pos:
            self.entered   = True
            self.short_leg = {**sell_opt, "entry": sell_pos["entry"]}
            self.hedge_leg = hedge_pos

    async def _monitor_positions(self):
        """Check 50% decay and delta breach"""
        if not self.short_leg:
            return

        # Find current LTP of short leg from chain
        current = next(
            (o for o in self.state.option_chain
             if o["symbol"] == self.short_leg["symbol"]), None)
        if not current:
            return

        entry_premium = self.short_leg["entry"]
        current_ltp   = current["ltp"]
        decay_pct     = 1 - (current_ltp / entry_premium) if entry_premium > 0 else 0

        # 50% decay exit
        if decay_pct >= self.state.config["decay_trigger_pct"]:
            self.state.add_log("GEKKO",
                f"50% DECAY TRIGGERED ({decay_pct:.0%}) on {self.short_leg['symbol']}. Exiting.", "trade")
            await self.order_mgr.close_all_positions()
            self.state.active_strategy = None
            self._reset()
            return

        # Delta breach → roll
        if (abs(current.get("delta", 0)) > self.state.config["adjustment_delta"]
                and self.state.roll_count < self.state.config["max_rolls"]):
            await self._roll(current)

    async def _roll(self, current_opt: dict):
        """Roll short leg 50pts further OTM"""
        self.state.roll_count += 1
        self.state.add_log("GEKKO",
            f"ROLL {self.state.roll_count}/{self.state.config['max_rolls']} | "
            f"Delta {current_opt['delta']:.2f} breached. Rolling 50pts.", "alert")

        # Close current short leg
        short_pos = next(
            (p for p in self.state.positions if p["symbol"] == self.short_leg["symbol"]), None)
        if short_pos:
            await self.order_mgr.close_position(short_pos)

        # Open new short leg 50pts further
        direction = 1 if current_opt["type"] == "CE" else -1
        new_strike = current_opt["strike"] + (50 * direction)
        new_symbol = f"NIFTY{current_opt['expiry']}{new_strike}{current_opt['type']}"

        try:
            q   = self.state.kite.quote([f"NFO:{new_symbol}"])[f"NFO:{new_symbol}"]
            ltp = q.get("last_price", 0)
        except Exception:
            return

        new_pos = await self.order_mgr.place_order(
            symbol=new_symbol, side="sell",
            qty=self.state.config["lot_size"],
            price=ltp, tag="A_ROLL"
        )
        if new_pos:
            self.short_leg = {**current_opt, "strike": new_strike,
                              "symbol": new_symbol, "entry": new_pos["entry"]}

    def _reset(self):
        self.entered   = False
        self.short_leg = None
        self.hedge_leg = None
        self.state.roll_count = 0


# ─────────────────────────────────────────────────────────────
# STRATEGY B — IRON CONDOR
# ─────────────────────────────────────────────────────────────
class StrategyB:
    """
    Entry:  IV Rank > 60. Sell CE spread + PE spread simultaneously.
            Short legs at Δ 0.20–0.30 on each side.
    Exit:   50% credit captured | target | SL | delta breach (roll)
    """
    def __init__(self, state, order_mgr):
        self.state     = state
        self.order_mgr = order_mgr
        self.scanner   = OptionScanner(state)
        self.entered   = False
        self.short_ce  = None
        self.short_pe  = None
        self._scan_counter = 0

    async def tick(self):
        self._scan_counter += 1
        if self._scan_counter % 60 == 0:
            await asyncio.get_event_loop().run_in_executor(
                None, self.scanner.fetch_and_update_chain)

        # Exits
        if self.state.target_hit():
            self.state.add_log("GEKKO",
                f"TARGET HIT ✓ | PnL: +₹{self.state.session_pnl:.0f}", "trade")
            await self.order_mgr.close_all_positions()
            self.state.active_strategy = None
            self._reset()
            return

        if self.state.sl_hit():
            self.state.add_log("GEKKO",
                f"STOP LOSS ✗ | PnL: ₹{self.state.session_pnl:.0f}", "alert")
            await self.order_mgr.close_all_positions()
            self.state.active_strategy = None
            self._reset()
            return

        if not self.entered:
            if self.state.iv_rank >= self.state.config["iv_rank_entry"]:
                chain = self.state.option_chain or await asyncio.get_event_loop().run_in_executor(
                    None, self.scanner.fetch_and_update_chain)
                await self._enter(chain)
            else:
                # Log once per minute
                if self._scan_counter % 60 == 0:
                    self.state.add_log("GEKKO",
                        f"Waiting for IV Rank > {self.state.config['iv_rank_entry']}. "
                        f"Current: {self.state.iv_rank}", "info")
            return

        await self._monitor()

    async def _enter(self, chain: list):
        dmin = self.state.config["delta_short_min"]
        dmax = self.state.config["delta_short_max"]
        lot  = self.state.config["lot_size"]
        wing = self.state.config["condor_wing_width"]

        # Find CE short leg
        ce_cands = [o for o in chain
                    if o["type"] == "CE" and dmin <= abs(o["delta"]) <= dmax]
        pe_cands = [o for o in chain
                    if o["type"] == "PE" and dmin <= abs(o["delta"]) <= dmax]

        if not ce_cands or not pe_cands:
            self.state.add_log("GEKKO", "Could not find suitable strikes for Iron Condor.", "alert")
            return

        sc  = ce_cands[0]  # short CE
        sp  = pe_cands[0]  # short PE
        expiry = sc["expiry"]

        lc_sym = f"NIFTY{expiry}{sc['strike'] + wing}CE"   # long CE
        lp_sym = f"NIFTY{expiry}{sp['strike'] - wing}PE"   # long PE

        try:
            lc_ltp = self.state.kite.quote([f"NFO:{lc_sym}"])[f"NFO:{lc_sym}"]["last_price"]
            lp_ltp = self.state.kite.quote([f"NFO:{lp_sym}"])[f"NFO:{lp_sym}"]["last_price"]
        except Exception:
            lc_ltp = lp_ltp = 5.0

        net_credit = round(sc["ltp"] + sp["ltp"] - lc_ltp - lp_ltp, 2)
        self.state.add_log("GEKKO",
            f"IRON CONDOR | SELL {sc['symbol']}@{sc['ltp']} + {sp['symbol']}@{sp['ltp']} | "
            f"Net credit: {net_credit}pts | IV Rank: {self.state.iv_rank}", "trade")

        # Place all 4 legs
        for sym, side, ltp, tag in [
            (sc["symbol"], "sell", sc["ltp"], "B_SHORT_CE"),
            (lc_sym,       "buy",  lc_ltp,   "B_LONG_CE"),
            (sp["symbol"], "sell", sp["ltp"], "B_SHORT_PE"),
            (lp_sym,       "buy",  lp_ltp,   "B_LONG_PE"),
        ]:
            await self.order_mgr.place_order(
                symbol=sym, side=side, qty=lot, price=ltp, tag=tag)
            await asyncio.sleep(0.5)  # small delay between legs

        self.entered  = True
        self.short_ce = sc
        self.short_pe = sp

    async def _monitor(self):
        """Adjust if short leg delta > threshold"""
        for short, opt_type in [(self.short_ce, "CE"), (self.short_pe, "PE")]:
            if not short:
                continue
            current = next(
                (o for o in self.state.option_chain if o["symbol"] == short["symbol"]), None)
            if not current:
                continue
            if abs(current.get("delta", 0)) > self.state.config["adjustment_delta"]:
                if self.state.roll_count < self.state.config["max_rolls"]:
                    await self._adjust_wing(current, opt_type)

    async def _adjust_wing(self, current: dict, opt_type: str):
        self.state.roll_count += 1
        self.state.add_log("GEKKO",
            f"CONDOR ADJUST {self.state.roll_count}/{self.state.config['max_rolls']} | "
            f"{opt_type} wing delta {current['delta']:.2f}. Moving 50pts.", "alert")

        # Close breached short leg
        pos = next(
            (p for p in self.state.positions if p["symbol"] == current["symbol"]), None)
        if pos:
            await self.order_mgr.close_position(pos)

        direction  = 1 if opt_type == "CE" else -1
        new_strike = current["strike"] + (50 * direction)
        new_symbol = f"NIFTY{current['expiry']}{new_strike}{opt_type}"

        try:
            ltp = self.state.kite.quote([f"NFO:{new_symbol}"])[f"NFO:{new_symbol}"]["last_price"]
        except Exception:
            return

        new_pos = await self.order_mgr.place_order(
            symbol=new_symbol, side="sell",
            qty=self.state.config["lot_size"],
            price=ltp, tag="B_ADJUST"
        )
        if new_pos:
            if opt_type == "CE":
                self.short_ce = {**current, "strike": new_strike, "symbol": new_symbol}
            else:
                self.short_pe = {**current, "strike": new_strike, "symbol": new_symbol}

    def _reset(self):
        self.entered  = False
        self.short_ce = None
        self.short_pe = None
        self.state.roll_count = 0
