"""
state.py — Shared mutable state for the GEKKO agent
Single source of truth for all components
"""
import datetime
from typing import Optional

class AgentState:
    def __init__(self):
        # Auth
        self.kite              = None          # KiteConnect instance
        self.auth_status       = "disconnected"
        self.token_expires     = None

        # Market data
        self.spot              = 0.0
        self.spot_ohlc         = {}
        self.vix               = 0.0
        self.iv_rank           = 0.0
        self.option_chain      = []            # list of option dicts with live Greeks

        # Strategy
        self.active_strategy   = None          # "A" | "B" | None
        self.positions         = []            # open legs
        self.session_pnl       = 0.0
        self.roll_count        = 0

        # Config (can be overridden via env vars)
        self.config = {
            "capital":                 float(__import__("os").getenv("CAPITAL", 500000)),
            "target_pct":              0.005,
            "sl_pct":                  0.005,
            "lot_size":                50,
            "max_legs":                6,
            "max_rolls":               5,
            "iv_mismatch_threshold":   2.0,
            "pre_noon_hedge_pts":      400,
            "post_noon_hedge_pts":     300,
            "decay_trigger_pct":       0.50,
            "iv_rank_entry":           60,
            "condor_wing_width":       100,
            "delta_short_min":         0.20,
            "delta_short_max":         0.30,
            "adjustment_delta":        0.45,
            "vix_52w_low":             10.0,
            "vix_52w_high":            35.0,
        }

        # Log
        self._log              = []

    # ── Helpers ──────────────────────────────────────────────
    def add_log(self, sender: str, text: str, type_: str = "info"):
        now = datetime.datetime.now()
        self._log.append({
            "sender": sender,
            "text":   text,
            "type":   type_,
            "time":   now.strftime("%H:%M"),
        })
        # Keep last 200 messages
        if len(self._log) > 200:
            self._log = self._log[-200:]

    def is_market_hours(self) -> bool:
        now = datetime.datetime.now().time()
        return datetime.time(9, 30) <= now <= datetime.time(15, 15)

    def target_hit(self) -> bool:
        cap = self.config["capital"]
        return self.session_pnl >= cap * self.config["target_pct"]

    def sl_hit(self) -> bool:
        cap = self.config["capital"]
        return self.session_pnl <= -(cap * self.config["sl_pct"])

    def snapshot(self) -> dict:
        """Full state snapshot sent to UI via WebSocket every second"""
        cap = self.config["capital"]
        return {
            "auth":          self.auth_status,
            "spot":          self.spot,
            "vix":           round(self.vix, 2),
            "iv_rank":       self.iv_rank,
            "session_pnl":   round(self.session_pnl, 2),
            "pnl_pct":       round(self.session_pnl / cap * 100, 3) if cap else 0,
            "target":        round(cap * self.config["target_pct"], 0),
            "sl":            round(cap * self.config["sl_pct"], 0),
            "active_strategy": self.active_strategy,
            "positions":     self.positions,
            "option_chain":  self.option_chain[:20],  # top 20 strikes
            "log":           self._log[-50:],          # last 50 messages
            "roll_count":    self.roll_count,
            "is_market":     self.is_market_hours(),
            "config":        self.config,
        }
