"""
orders.py — Zerodha order placement and position tracking
Handles: LIMIT orders, IOC fallback to MARKET, position squareoff
"""
import asyncio, logging, datetime
from kiteconnect import KiteConnect

log = logging.getLogger("GEKKO.orders")

class OrderManager:
    def __init__(self, state):
        self.state = state

    @property
    def kite(self) -> KiteConnect:
        return self.state.kite

    async def place_order(self,
        symbol: str,
        side: str,        # "buy" | "sell"
        qty: int,
        price: float,
        token: int = None,
        tag: str = "",
    ) -> dict | None:
        """
        Place LIMIT order. If not filled in 10s, cancel and retry as MARKET.
        Returns position dict on success, None on failure.
        """
        txn = self.kite.TRANSACTION_TYPE_BUY if side == "buy" else self.kite.TRANSACTION_TYPE_SELL

        try:
            # Try LIMIT first (better price)
            order_id = self.kite.place_order(
                variety  = self.kite.VARIETY_REGULAR,
                exchange = self.kite.EXCHANGE_NFO,
                tradingsymbol = symbol,
                transaction_type = txn,
                quantity   = qty,
                order_type = self.kite.ORDER_TYPE_LIMIT,
                price      = price,
                product    = self.kite.PRODUCT_MIS,
                tag        = f"GEKKO_{tag}"
            )
            log.info(f"LIMIT order placed: {side.upper()} {symbol} @ {price} | id: {order_id}")
            self.state.add_log("GEKKO",
                f"ORDER → {side.upper()} {symbol} @ ₹{price} (LIMIT)", "trade")

            # Wait up to 10s for fill
            filled_price = await self._wait_for_fill(order_id, timeout=10)

            if filled_price is None:
                # Cancel and go MARKET
                log.warning(f"LIMIT not filled, cancelling and retrying MARKET: {order_id}")
                try:
                    self.kite.cancel_order(self.kite.VARIETY_REGULAR, order_id)
                except Exception:
                    pass
                filled_price = await self._place_market(symbol, txn, qty, tag)

            if filled_price is None:
                self.state.add_log("GEKKO", f"ORDER FAILED: {symbol}", "alert")
                return None

            pos = {
                "symbol":   symbol,
                "token":    token,
                "side":     side,
                "qty":      qty,
                "entry":    filled_price,
                "ltp":      filled_price,
                "pnl":      0.0,
                "order_id": order_id,
                "time":     datetime.datetime.now().strftime("%H:%M:%S"),
            }
            self.state.positions.append(pos)
            self.state.add_log("GEKKO",
                f"FILLED: {side.upper()} {symbol} @ ₹{filled_price:.2f}", "trade")
            return pos

        except Exception as e:
            log.error(f"Order placement error: {e}")
            self.state.add_log("GEKKO", f"Order error: {e}", "alert")
            return None

    async def _place_market(self, symbol: str, txn, qty: int, tag: str) -> float | None:
        """Fallback MARKET order"""
        try:
            order_id = self.kite.place_order(
                variety  = self.kite.VARIETY_REGULAR,
                exchange = self.kite.EXCHANGE_NFO,
                tradingsymbol = symbol,
                transaction_type = txn,
                quantity   = qty,
                order_type = self.kite.ORDER_TYPE_MARKET,
                product    = self.kite.PRODUCT_MIS,
                tag        = f"GEKKO_{tag}_MKT"
            )
            log.info(f"MARKET order placed: {symbol} | id: {order_id}")
            return await self._wait_for_fill(order_id, timeout=15)
        except Exception as e:
            log.error(f"Market order error: {e}")
            return None

    async def _wait_for_fill(self, order_id: str, timeout: int = 10) -> float | None:
        """Poll order book until filled or timeout"""
        for _ in range(timeout):
            await asyncio.sleep(1)
            try:
                orders = self.kite.orders()
                for o in orders:
                    if str(o["order_id"]) == str(order_id):
                        if o["status"] == "COMPLETE":
                            return float(o["average_price"])
                        if o["status"] in ("REJECTED", "CANCELLED"):
                            log.warning(f"Order {order_id} {o['status']}: {o.get('status_message')}")
                            return None
            except Exception as e:
                log.error(f"Order status poll error: {e}")
        return None

    async def close_position(self, pos: dict):
        """Close a single position"""
        close_side = "sell" if pos["side"] == "buy" else "buy"
        txn = self.kite.TRANSACTION_TYPE_SELL if close_side == "sell" else self.kite.TRANSACTION_TYPE_BUY
        try:
            order_id = self.kite.place_order(
                variety  = self.kite.VARIETY_REGULAR,
                exchange = self.kite.EXCHANGE_NFO,
                tradingsymbol = pos["symbol"],
                transaction_type = txn,
                quantity   = pos["qty"],
                order_type = self.kite.ORDER_TYPE_MARKET,
                product    = self.kite.PRODUCT_MIS,
                tag        = "GEKKO_CLOSE"
            )
            log.info(f"Close order: {close_side.upper()} {pos['symbol']} | id: {order_id}")
            self.state.add_log("GEKKO",
                f"CLOSE → {close_side.upper()} {pos['symbol']} @ MARKET", "trade")
            if pos in self.state.positions:
                self.state.positions.remove(pos)
        except Exception as e:
            log.error(f"Close position error: {e}")

    async def close_all_positions(self):
        """Square off all open positions"""
        positions = list(self.state.positions)
        for pos in positions:
            await self.close_position(pos)
        self.state.add_log("GEKKO", f"All {len(positions)} legs closed.", "trade")

    def get_instrument_token(self, symbol: str, exchange: str = "NFO") -> int | None:
        """Lookup instrument token for a symbol"""
        try:
            instruments = self.kite.instruments(exchange)
            for inst in instruments:
                if inst["tradingsymbol"] == symbol:
                    return inst["instrument_token"]
        except Exception as e:
            log.error(f"Token lookup error: {e}")
        return None
