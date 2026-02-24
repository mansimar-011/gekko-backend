"""
GEKKO Backend — FastAPI + WebSocket + Zerodha KiteConnect
Handles: auto-login, live prices, order placement, strategy execution
"""
import os, json, asyncio, logging, datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

from auth import ZerodhaAuth
from market import MarketFeed
from strategies import StrategyA, StrategyB
from orders import OrderManager
from state import AgentState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [GEKKO] %(message)s")
log = logging.getLogger("GEKKO")

# ── Shared state ──────────────────────────────────────────────
state = AgentState()
auth  = ZerodhaAuth(state)
feed  = MarketFeed(state)
order_mgr = OrderManager(state)
strat_a   = StrategyA(state, order_mgr)
strat_b   = StrategyB(state, order_mgr)

# ── Lifespan: startup tasks ───────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("GEKKO starting up...")
    asyncio.create_task(auth.auto_login_loop())      # handles daily token
    asyncio.create_task(feed.ticker_loop())          # live prices via KiteTicker
    asyncio.create_task(strategy_monitor_loop())     # checks strategies every second
    asyncio.create_task(broadcast_loop())            # pushes data to all WS clients
    yield
    log.info("GEKKO shutting down...")
    await feed.stop()

app = FastAPI(title="GEKKO API", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── WebSocket connection manager ──────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info(f"WS client connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

mgr = ConnectionManager()

# ── Background loops ──────────────────────────────────────────
async def strategy_monitor_loop():
    """Runs active strategy logic every second during market hours"""
    while True:
        await asyncio.sleep(1)
        if not state.is_market_hours() or not state.kite:
            continue
        try:
            if state.active_strategy == "A":
                await strat_a.tick()
            elif state.active_strategy == "B":
                await strat_b.tick()
        except Exception as e:
            log.error(f"Strategy tick error: {e}")

async def broadcast_loop():
    """Pushes snapshot to all connected UI clients every second"""
    while True:
        await asyncio.sleep(1)
        if not mgr.active:
            continue
        try:
            snapshot = state.snapshot()
            await mgr.broadcast({"type": "snapshot", "data": snapshot})
        except Exception as e:
            log.error(f"Broadcast error: {e}")

# ── REST endpoints ────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "GEKKO running", "auth": state.auth_status, "strategy": state.active_strategy}

@app.get("/login")
async def login_redirect():
    """Step 1: redirect to Zerodha login page"""
    url = auth.get_login_url()
    return RedirectResponse(url)

@app.get("/callback")
async def zerodha_callback(request_token: str):
    """Step 2: Zerodha redirects here with request_token"""
    try:
        await auth.exchange_token(request_token)
        return HTMLResponse(LOGIN_SUCCESS_HTML)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth-status")
async def auth_status():
    return {"status": state.auth_status, "expires": state.token_expires}

@app.post("/strategy/start/{name}")
async def start_strategy(name: str):
    if name not in ("A", "B"):
        raise HTTPException(400, "Strategy must be A or B")
    if not state.kite:
        raise HTTPException(400, "Not authenticated with Zerodha")
    if state.active_strategy:
        raise HTTPException(400, f"Strategy {state.active_strategy} already running")
    state.active_strategy = name
    state.add_log("GEKKO", f"Strategy {name} activated.", "info")
    return {"started": name}

@app.post("/strategy/stop")
async def stop_strategy():
    name = state.active_strategy
    if name:
        await order_mgr.close_all_positions()
        state.active_strategy = None
        state.add_log("GEKKO", "All positions closed. Strategy stopped.", "alert")
    return {"stopped": name}

@app.get("/positions")
async def get_positions():
    return {"positions": state.positions, "pnl": state.session_pnl}

@app.get("/chain")
async def get_chain():
    """Live option chain with Greeks"""
    return {"chain": state.option_chain, "spot": state.spot, "vix": state.vix}

# ── WebSocket endpoint ────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await mgr.connect(ws)
    # Send initial state immediately on connect
    await ws.send_json({"type": "snapshot", "data": state.snapshot()})
    try:
        while True:
            msg = await ws.receive_json()
            await handle_ws_message(msg, ws)
    except WebSocketDisconnect:
        mgr.disconnect(ws)

async def handle_ws_message(msg: dict, ws: WebSocket):
    """Handle commands from the UI"""
    cmd = msg.get("cmd", "")
    if cmd == "start_strategy":
        name = msg.get("strategy", "A")
        if not state.kite:
            await ws.send_json({"type": "error", "msg": "Not authenticated. Login first."})
            return
        state.active_strategy = name
        state.add_log("GEKKO", f"Strategy {name} activated by user.", "info")
    elif cmd == "stop":
        await order_mgr.close_all_positions()
        state.active_strategy = None
        state.add_log("GEKKO", "Manual stop. All positions closed.", "alert")
    elif cmd == "scan":
        state.add_log("GEKKO",
            f"Spot: {state.spot} | VIX: {state.vix:.1f} | IV Rank: {state.iv_rank} | "
            f"Legs: {len(state.positions)} | PnL: ₹{state.session_pnl:.0f}", "info")

# ── Login success page ────────────────────────────────────────
LOGIN_SUCCESS_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body{background:#06090a;color:#00ffc8;font-family:monospace;display:flex;
         align-items:center;justify-content:center;height:100vh;margin:0;flex-direction:column;gap:16px}
    h2{letter-spacing:3px;font-size:18px}
    p{color:#555;font-size:12px;letter-spacing:1px}
    .dot{width:12px;height:12px;border-radius:50%;background:#00ffc8;
         animation:pulse 1.5s infinite;margin:0 auto}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.2}}
  </style>
</head>
<body>
  <div class="dot"></div>
  <h2>✓ ZERODHA CONNECTED</h2>
  <p>Token saved. You can close this tab.</p>
  <p>GEKKO is now live.</p>
  <script>
    // Auto-close after 3 seconds on iPhone
    setTimeout(() => window.close(), 3000);
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
