"""
options_paper_engine.py
=======================
Full options paper trading engine.
Supports: Buy/Sell CE/PE, SL/TP on premium, margin simulation,
multi-leg positions, expiry auto-settlement.

Used by: bot_server.py, options_monitor.py, options_strategies.py
"""

import json
import os
import logging
from datetime import datetime, date
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────

LOT_SIZE    = {"NIFTY": 50, "BANKNIFTY": 30, "SENSEX": 20}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}

# Margin blocked per lot for naked sells (simplified SPAN approximation)
MARGIN_PER_LOT = {"NIFTY": 80000, "BANKNIFTY": 50000, "SENSEX": 60000}

DEFAULT_CAPITAL = 500000.0   # ₹5 lakh starting capital
STATE_FILE      = "options_paper_account.json"

DEFAULT_STATE = {
    "capital":       DEFAULT_CAPITAL,
    "used_margin":   0.0,
    "realized_pnl":  0.0,
    "open_positions": {},    # key → position dict
    "closed_positions": [],
    "trade_log": [],
}


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _pos_key(underlying: str, strike: int, opt_type: str, expiry: str) -> str:
    """Unique key per option contract."""
    return f"{underlying}_{strike}{opt_type}_{expiry}"


def _underlying(symbol: str) -> str:
    """Extract underlying from symbol like NIFTY_24500CE_26JUN."""
    for u in LOT_SIZE:
        if symbol.startswith(u):
            return u
    return "NIFTY"


def _lot_size(underlying: str) -> int:
    return LOT_SIZE.get(underlying, 50)


def _margin_per_lot(underlying: str) -> float:
    return MARGIN_PER_LOT.get(underlying, 80000)


# ─── ENGINE ──────────────────────────────────────────────────────────────────

class OptionsPaperEngine:

    def __init__(self):
        self._lock = Lock()
        self.state = self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not os.path.exists(STATE_FILE):
            s = DEFAULT_STATE.copy()
            s["open_positions"] = {}
            s["closed_positions"] = []
            s["trade_log"] = []
            self._save_raw(s)
            return s
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"[OPE] load failed: {e}. Fresh state.")
            return DEFAULT_STATE.copy()

    def _save_raw(self, state: dict):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as e:
            log.warning(f"[OPE] save failed: {e}")

    def _save(self):
        self._save_raw(self.state)

    # ── Account info ─────────────────────────────────────────────────────────

    def get_available_capital(self) -> float:
        return self.state["capital"] - self.state["used_margin"]

    def get_summary(self) -> dict:
        open_pnl = sum(
            p.get("unrealized_pnl", 0)
            for p in self.state["open_positions"].values()
        )
        return {
            "capital":        self.state["capital"],
            "used_margin":    self.state["used_margin"],
            "available":      self.get_available_capital(),
            "open_pnl":       open_pnl,
            "realized_pnl":   self.state["realized_pnl"],
            "net_pnl":        self.state["realized_pnl"] + open_pnl,
            "open_positions": len(self.state["open_positions"]),
        }

    # ── Place order ───────────────────────────────────────────────────────────

    def place_order(
        self,
        underlying: str,    # NIFTY / BANKNIFTY / SENSEX
        strike: int,
        opt_type: str,      # CE / PE
        expiry: str,        # e.g. "26JUN"
        action: str,        # BUY / SELL
        lots: int,
        premium: float,     # current LTP
        strategy_tag: str = "",   # optional: "iron_condor", "straddle" etc
        leg_tag: str = "",        # optional: "short_ce", "long_pe" etc
    ) -> tuple[bool, str]:

        underlying = underlying.upper()
        opt_type   = opt_type.upper()
        action     = action.upper()

        if underlying not in LOT_SIZE:
            return False, f"Unknown underlying: {underlying}"
        if opt_type not in ("CE", "PE"):
            return False, "opt_type must be CE or PE"
        if action not in ("BUY", "SELL"):
            return False, "action must be BUY or SELL"
        if lots < 1:
            return False, "lots must be >= 1"
        if premium <= 0:
            return False, "premium must be > 0"

        lot_sz  = _lot_size(underlying)
        qty     = lots * lot_sz
        key     = _pos_key(underlying, strike, opt_type, expiry)

        with self._lock:
            # ── BUY: deduct premium cost ──────────────────────────────────
            if action == "BUY":
                cost = premium * qty
                if cost > self.get_available_capital():
                    return False, f"Insufficient capital. Need ₹{cost:.0f}, have ₹{self.get_available_capital():.0f}"

                # If position already open (averaging / adding), handle gracefully
                if key in self.state["open_positions"]:
                    pos = self.state["open_positions"][key]
                    if pos["action"] == "BUY":
                        # Average up — compute new avg entry
                        old_qty   = pos["qty"]
                        old_entry = pos["entry_premium"]
                        new_qty   = old_qty + qty
                        avg_entry = (old_entry * old_qty + premium * qty) / new_qty
                        pos["qty"]           = new_qty
                        pos["lots"]          += lots
                        pos["entry_premium"] = round(avg_entry, 2)
                        self.state["capital"] -= cost
                        self._save()
                        return True, (
                            f"✅ Added to BUY {underlying} {strike}{opt_type} {expiry}\n"
                            f"Avg entry: ₹{avg_entry:.2f} | Total qty: {new_qty} | Cost: ₹{cost:.0f}"
                        )
                    else:
                        return False, "Position exists as SELL. Close it first."

                self.state["capital"] -= cost
                self.state["open_positions"][key] = {
                    "key":            key,
                    "underlying":     underlying,
                    "strike":         strike,
                    "opt_type":       opt_type,
                    "expiry":         expiry,
                    "action":         "BUY",
                    "lots":           lots,
                    "qty":            qty,
                    "lot_size":       lot_sz,
                    "entry_premium":  round(premium, 2),
                    "ltp":            round(premium, 2),
                    "sl":             None,
                    "tp":             None,
                    "unrealized_pnl": 0.0,
                    "strategy_tag":   strategy_tag,
                    "leg_tag":        leg_tag,
                    "opened_at":      str(datetime.now()),
                    "margin_blocked": 0.0,
                }
                self._save()
                return True, (
                    f"✅ BUY {underlying} {strike}{opt_type} {expiry}\n"
                    f"Premium: ₹{premium} | Lots: {lots} | Qty: {qty}\n"
                    f"Cost: ₹{cost:.0f} | Capital left: ₹{self.get_available_capital():.0f}"
                )

            # ── SELL: block margin, receive premium ───────────────────────
            else:
                margin_needed = _margin_per_lot(underlying) * lots
                credit        = premium * qty

                if margin_needed > self.get_available_capital():
                    return False, (
                        f"Insufficient margin. Need ₹{margin_needed:.0f}, "
                        f"have ₹{self.get_available_capital():.0f}"
                    )

                if key in self.state["open_positions"]:
                    return False, "SELL position already open for this contract. Close first."

                self.state["used_margin"] += margin_needed
                # Credit received added to capital
                self.state["capital"] += credit

                self.state["open_positions"][key] = {
                    "key":            key,
                    "underlying":     underlying,
                    "strike":         strike,
                    "opt_type":       opt_type,
                    "expiry":         expiry,
                    "action":         "SELL",
                    "lots":           lots,
                    "qty":            qty,
                    "lot_size":       lot_sz,
                    "entry_premium":  round(premium, 2),
                    "ltp":            round(premium, 2),
                    "sl":             None,
                    "tp":             None,
                    "unrealized_pnl": 0.0,
                    "strategy_tag":   strategy_tag,
                    "leg_tag":        leg_tag,
                    "opened_at":      str(datetime.now()),
                    "margin_blocked": margin_needed,
                }
                self._save()
                return True, (
                    f"✅ SELL {underlying} {strike}{opt_type} {expiry}\n"
                    f"Premium received: ₹{credit:.0f} | Lots: {lots} | Qty: {qty}\n"
                    f"Margin blocked: ₹{margin_needed:.0f} | Max profit: ₹{credit:.0f}"
                )

    # ── Set SL / TP on premium ────────────────────────────────────────────────

    def set_sl(self, key: str, sl_premium: float) -> tuple[bool, str]:
        with self._lock:
            if key not in self.state["open_positions"]:
                return False, f"No position found: {key}"
            pos = self.state["open_positions"][key]
            pos["sl"] = round(sl_premium, 2)
            self._save()
            return True, f"SL set at premium ₹{sl_premium:.2f} for {key}"

    def set_tp(self, key: str, tp_premium: float) -> tuple[bool, str]:
        with self._lock:
            if key not in self.state["open_positions"]:
                return False, f"No position found: {key}"
            pos = self.state["open_positions"][key]
            pos["tp"] = round(tp_premium, 2)
            self._save()
            return True, f"TP set at premium ₹{tp_premium:.2f} for {key}"

    # ── Update LTP (called by monitor thread) ────────────────────────────────

    def update_ltp(self, key: str, ltp: float) -> Optional[str]:
        """
        Update LTP for a position. Returns trigger message if SL/TP hit, else None.
        Called by options_monitor.py every tick.
        """
        with self._lock:
            if key not in self.state["open_positions"]:
                return None
            pos = self.state["open_positions"][key]
            pos["ltp"] = round(ltp, 2)

            # PnL calculation
            qty    = pos["qty"]
            entry  = pos["entry_premium"]
            action = pos["action"]

            if action == "BUY":
                pos["unrealized_pnl"] = round((ltp - entry) * qty, 2)
            else:
                # SELL: profit when premium falls
                pos["unrealized_pnl"] = round((entry - ltp) * qty, 2)

            self._save()

            # Check SL/TP triggers
            sl = pos.get("sl")
            tp = pos.get("tp")

            if action == "BUY":
                if sl and ltp <= sl:
                    return f"SL_HIT:{key}:{ltp}"
                if tp and ltp >= tp:
                    return f"TP_HIT:{key}:{ltp}"
            else:  # SELL
                # For sells: SL = premium rises above threshold (loss)
                #            TP = premium falls below threshold (profit)
                if sl and ltp >= sl:
                    return f"SL_HIT:{key}:{ltp}"
                if tp and ltp <= tp:
                    return f"TP_HIT:{key}:{ltp}"

        return None

    # ── Close position ────────────────────────────────────────────────────────

    def close_position(self, key: str, exit_premium: float, reason: str = "manual") -> tuple[bool, str]:
        with self._lock:
            if key not in self.state["open_positions"]:
                return False, f"No open position: {key}"

            pos    = self.state["open_positions"][key]
            qty    = pos["qty"]
            entry  = pos["entry_premium"]
            action = pos["action"]
            lots   = pos["lots"]
            underlying = pos["underlying"]

            if action == "BUY":
                # Pay exit premium to sell
                pnl = (exit_premium - entry) * qty
                self.state["capital"] += exit_premium * qty
            else:
                # Buy back to close sell
                buyback_cost = exit_premium * qty
                self.state["capital"] -= buyback_cost
                pnl = (entry - exit_premium) * qty
                # Release margin
                self.state["used_margin"] = max(
                    0, self.state["used_margin"] - pos["margin_blocked"]
                )

            pnl = round(pnl, 2)
            self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 2)

            closed = dict(pos)
            closed["exit_premium"] = round(exit_premium, 2)
            closed["realized_pnl"] = pnl
            closed["close_reason"] = reason
            closed["closed_at"]    = str(datetime.now())
            self.state["closed_positions"].append(closed)

            # Trade log entry
            self.state["trade_log"].append({
                "date":           date.today().isoformat(),
                "key":            key,
                "action":         action,
                "underlying":     underlying,
                "strike":         pos["strike"],
                "opt_type":       pos["opt_type"],
                "expiry":         pos["expiry"],
                "lots":           lots,
                "entry_premium":  entry,
                "exit_premium":   round(exit_premium, 2),
                "pnl":            pnl,
                "result":         "WIN" if pnl >= 0 else "LOSS",
                "reason":         reason,
                "strategy_tag":   pos.get("strategy_tag", ""),
                "opened_at":      pos["opened_at"],
                "closed_at":      closed["closed_at"],
            })

            del self.state["open_positions"][key]
            self._save()

            emoji = "🟢" if pnl >= 0 else "🔴"
            return True, (
                f"{emoji} CLOSED {pos['underlying']} {pos['strike']}{pos['opt_type']} {pos['expiry']}\n"
                f"Entry: ₹{entry} → Exit: ₹{exit_premium:.2f}\n"
                f"PnL: ₹{pnl:+.0f} | Reason: {reason}"
            )

    # ── Close all legs of a strategy ─────────────────────────────────────────

    def close_strategy(self, strategy_tag: str, ltps: dict) -> list[str]:
        """
        Close all positions with matching strategy_tag.
        ltps = {key: current_ltp}
        """
        msgs = []
        keys = [
            k for k, p in self.state["open_positions"].items()
            if p.get("strategy_tag") == strategy_tag
        ]
        for key in keys:
            ltp = ltps.get(key, self.state["open_positions"][key]["ltp"])
            ok, msg = self.close_position(key, ltp, reason=f"close_strategy:{strategy_tag}")
            msgs.append(msg)
        return msgs

    # ── Expiry settlement ─────────────────────────────────────────────────────

    def settle_expiry(self, expiry: str, spot_price: float) -> list[str]:
        """
        Called at 3:30pm on expiry day.
        Options expire worthless or at intrinsic value.
        """
        msgs = []
        keys = [
            k for k, p in self.state["open_positions"].items()
            if p["expiry"].upper() == expiry.upper()
        ]
        for key in keys:
            pos = self.state["open_positions"][key]
            strike     = pos["strike"]
            opt_type   = pos["opt_type"]
            intrinsic  = 0.0

            if opt_type == "CE":
                intrinsic = max(0.0, spot_price - strike)
            else:
                intrinsic = max(0.0, strike - spot_price)

            ok, msg = self.close_position(key, intrinsic, reason="expiry_settlement")
            msgs.append(msg)
            log.info(f"[OPE] Expiry settle {key}: intrinsic={intrinsic:.2f}")

        return msgs

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        return list(self.state["open_positions"].values())

    def get_position(self, key: str) -> Optional[dict]:
        return self.state["open_positions"].get(key)

    def get_all_keys(self) -> list[str]:
        return list(self.state["open_positions"].keys())

    def get_trade_log(self, limit: int = 10) -> list[dict]:
        return self.state["trade_log"][-limit:]

    def get_daily_pnl(self) -> float:
        today = date.today().isoformat()
        return sum(
            t["pnl"] for t in self.state["trade_log"]
            if t.get("date") == today
        )

    def reset(self, capital: float = DEFAULT_CAPITAL):
        """Full reset — wipe all positions, restore capital."""
        with self._lock:
            self.state = {
                "capital":          capital,
                "used_margin":      0.0,
                "realized_pnl":     0.0,
                "open_positions":   {},
                "closed_positions": [],
                "trade_log":        [],
            }
            self._save()
        return f"✅ Account reset. Capital: ₹{capital:,.0f}"


# ─── Singleton ────────────────────────────────────────────────────────────────

_engine: Optional[OptionsPaperEngine] = None

def get_engine() -> OptionsPaperEngine:
    global _engine
    if _engine is None:
        _engine = OptionsPaperEngine()
    return _engine