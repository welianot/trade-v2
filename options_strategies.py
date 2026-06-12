"""
options_strategies.py
=====================
Multi-leg strategy builders. Each returns list of legs to place.
Engine places all legs atomically.

Improvements over v1:
  - Per-underlying configurable short/wing offsets (iron_condor safer on BANKNIFTY)
  - strangle default offset = 1 (was 2 — too wide for low IV days)
  - IV guard passed through to engine (rejects sells below MIN_IV)
  - Strategy PnL display helper
  - Rollback logs warning if position not found

Strategies:
  - straddle        (sell/buy ATM CE + PE)
  - strangle        (sell/buy OTM CE + PE)
  - bull_call       (buy CE + sell higher CE)
  - bear_put        (buy PE + sell lower PE)
  - bull_put        (sell PE + buy lower PE) — credit
  - bear_call       (sell CE + buy higher CE) — credit
  - iron_condor     (bull_put + bear_call combined)
  - iron_butterfly  (sell ATM straddle + buy wings)
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

LOT_SIZE    = {"NIFTY": 50, "BANKNIFTY": 30, "SENSEX": 20}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}

# Per-underlying iron_condor offsets
# short_offset: strikes away from ATM for short legs
# wing_offset:  strikes away from ATM for long legs (hedge)
CONDOR_CONFIG = {
    "NIFTY":     {"short_offset": 1, "wing_offset": 3},
    "BANKNIFTY": {"short_offset": 2, "wing_offset": 4},  # wider — BANKNIFTY moves fast
    "SENSEX":    {"short_offset": 2, "wing_offset": 4},
}

# Default strangle offset per underlying (1 = 1 strike OTM)
STRANGLE_OFFSET = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 1}


def _atm(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def _otm_ce(spot: float, step: int, n: int = 1) -> int:
    return _atm(spot, step) + step * n


def _otm_pe(spot: float, step: int, n: int = 1) -> int:
    return _atm(spot, step) - step * n


# ─── Strategy builders ────────────────────────────────────────────────────────

def straddle(underlying: str, spot: float, lots: int = 1, action: str = "SELL") -> list[dict]:
    """Sell/Buy ATM CE + ATM PE."""
    step = STRIKE_STEP[underlying]
    atm  = _atm(spot, step)
    tag  = f"straddle_{atm}_{action.lower()}"
    return [
        {"underlying": underlying, "strike": atm, "opt_type": "CE",
         "action": action, "lots": lots, "leg_tag": f"{action.lower()}_ce", "strategy_tag": tag},
        {"underlying": underlying, "strike": atm, "opt_type": "PE",
         "action": action, "lots": lots, "leg_tag": f"{action.lower()}_pe", "strategy_tag": tag},
    ]


def strangle(
    underlying: str,
    spot: float,
    lots: int = 1,
    action: str = "SELL",
    ce_offset: Optional[int] = None,
    pe_offset: Optional[int] = None,
) -> list[dict]:
    """
    Sell/Buy OTM CE + OTM PE.
    Default offset = 1 strike OTM (tighter than v1 default of 2).
    Override per-underlying via STRANGLE_OFFSET or pass ce_offset/pe_offset.
    """
    step      = STRIKE_STEP[underlying]
    default_n = STRANGLE_OFFSET.get(underlying, 1)
    ce_n      = ce_offset if ce_offset is not None else default_n
    pe_n      = pe_offset if pe_offset is not None else default_n
    ce_str    = _otm_ce(spot, step, ce_n)
    pe_str    = _otm_pe(spot, step, pe_n)
    tag       = f"strangle_{ce_str}_{pe_str}_{action.lower()}"
    return [
        {"underlying": underlying, "strike": ce_str, "opt_type": "CE",
         "action": action, "lots": lots, "leg_tag": f"{action.lower()}_ce", "strategy_tag": tag},
        {"underlying": underlying, "strike": pe_str, "opt_type": "PE",
         "action": action, "lots": lots, "leg_tag": f"{action.lower()}_pe", "strategy_tag": tag},
    ]


def bull_call_spread(underlying: str, spot: float, lots: int = 1) -> list[dict]:
    """Buy ATM CE + Sell 1-OTM CE. Bullish debit spread."""
    step    = STRIKE_STEP[underlying]
    buy_str = _atm(spot, step)
    sel_str = buy_str + step
    tag     = f"bull_call_{buy_str}_{sel_str}"
    return [
        {"underlying": underlying, "strike": buy_str, "opt_type": "CE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_ce",  "strategy_tag": tag},
        {"underlying": underlying, "strike": sel_str, "opt_type": "CE",
         "action": "SELL", "lots": lots, "leg_tag": "short_ce", "strategy_tag": tag},
    ]


def bear_put_spread(underlying: str, spot: float, lots: int = 1) -> list[dict]:
    """Buy ATM PE + Sell 1-OTM PE. Bearish debit spread."""
    step    = STRIKE_STEP[underlying]
    buy_str = _atm(spot, step)
    sel_str = buy_str - step
    tag     = f"bear_put_{buy_str}_{sel_str}"
    return [
        {"underlying": underlying, "strike": buy_str, "opt_type": "PE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_pe",  "strategy_tag": tag},
        {"underlying": underlying, "strike": sel_str, "opt_type": "PE",
         "action": "SELL", "lots": lots, "leg_tag": "short_pe", "strategy_tag": tag},
    ]


def bull_put_spread(underlying: str, spot: float, lots: int = 1) -> list[dict]:
    """Sell ATM PE + Buy 1-OTM PE. Credit spread. Bullish."""
    step    = STRIKE_STEP[underlying]
    sel_str = _atm(spot, step)
    buy_str = sel_str - step
    tag     = f"bull_put_{sel_str}_{buy_str}"
    return [
        {"underlying": underlying, "strike": sel_str, "opt_type": "PE",
         "action": "SELL", "lots": lots, "leg_tag": "short_pe", "strategy_tag": tag},
        {"underlying": underlying, "strike": buy_str, "opt_type": "PE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_pe",  "strategy_tag": tag},
    ]


def bear_call_spread(underlying: str, spot: float, lots: int = 1) -> list[dict]:
    """Sell ATM CE + Buy 1-OTM CE. Credit spread. Bearish."""
    step    = STRIKE_STEP[underlying]
    sel_str = _atm(spot, step)
    buy_str = sel_str + step
    tag     = f"bear_call_{sel_str}_{buy_str}"
    return [
        {"underlying": underlying, "strike": sel_str, "opt_type": "CE",
         "action": "SELL", "lots": lots, "leg_tag": "short_ce", "strategy_tag": tag},
        {"underlying": underlying, "strike": buy_str, "opt_type": "CE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_ce",  "strategy_tag": tag},
    ]


def iron_condor(
    underlying: str,
    spot: float,
    lots: int = 1,
    short_offset: Optional[int] = None,
    wing_offset: Optional[int] = None,
) -> list[dict]:
    """
    Bull put spread (below) + Bear call spread (above).
    4 legs. Neutral strategy. Max profit in range.

    Per-underlying defaults via CONDOR_CONFIG:
      NIFTY:     short=1, wing=3
      BANKNIFTY: short=2, wing=4  (wider — faster moving index)
      SENSEX:    short=2, wing=4
    """
    step   = STRIKE_STEP[underlying]
    atm    = _atm(spot, step)
    cfg    = CONDOR_CONFIG.get(underlying, {"short_offset": 1, "wing_offset": 3})
    s_off  = short_offset if short_offset is not None else cfg["short_offset"]
    w_off  = wing_offset  if wing_offset  is not None else cfg["wing_offset"]

    if w_off <= s_off:
        raise ValueError(f"wing_offset ({w_off}) must be > short_offset ({s_off})")

    short_ce = atm + step * s_off
    long_ce  = atm + step * w_off
    short_pe = atm - step * s_off
    long_pe  = atm - step * w_off

    tag = f"iron_condor_{long_pe}_{short_pe}_{short_ce}_{long_ce}"
    return [
        {"underlying": underlying, "strike": short_ce, "opt_type": "CE",
         "action": "SELL", "lots": lots, "leg_tag": "short_ce", "strategy_tag": tag},
        {"underlying": underlying, "strike": long_ce,  "opt_type": "CE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_ce",  "strategy_tag": tag},
        {"underlying": underlying, "strike": short_pe, "opt_type": "PE",
         "action": "SELL", "lots": lots, "leg_tag": "short_pe", "strategy_tag": tag},
        {"underlying": underlying, "strike": long_pe,  "opt_type": "PE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_pe",  "strategy_tag": tag},
    ]


def iron_butterfly(
    underlying: str,
    spot: float,
    lots: int = 1,
    wing_offset: int = 2,
) -> list[dict]:
    """
    Sell ATM straddle + Buy OTM wings.
    4 legs. High premium collect. Neutral, low movement expected.
    """
    step     = STRIKE_STEP[underlying]
    atm      = _atm(spot, step)
    long_ce  = atm + step * wing_offset
    long_pe  = atm - step * wing_offset
    tag      = f"iron_butterfly_{atm}"
    return [
        {"underlying": underlying, "strike": atm,     "opt_type": "CE",
         "action": "SELL", "lots": lots, "leg_tag": "short_atm_ce", "strategy_tag": tag},
        {"underlying": underlying, "strike": atm,     "opt_type": "PE",
         "action": "SELL", "lots": lots, "leg_tag": "short_atm_pe", "strategy_tag": tag},
        {"underlying": underlying, "strike": long_ce, "opt_type": "CE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_ce",      "strategy_tag": tag},
        {"underlying": underlying, "strike": long_pe, "opt_type": "PE",
         "action": "BUY",  "lots": lots, "leg_tag": "long_pe",      "strategy_tag": tag},
    ]


# ─── Strategy placer ─────────────────────────────────────────────────────────

def place_strategy(
    engine,
    legs: list[dict],
    expiry: str,
    premiums: dict,
    iv: Optional[float] = None,
) -> tuple[bool, list[str]]:
    """
    Place all legs of a strategy atomically.
    premiums = {(strike, opt_type): ltp}
    iv = current IV% (optional — passed to engine for sell validation)
    Returns (all_ok, messages)
    """
    msgs   = []
    placed = []

    for leg in legs:
        strike   = leg["strike"]
        opt_type = leg["opt_type"]
        action   = leg["action"]
        premium  = premiums.get((strike, opt_type))

        if premium is None or premium <= 0:
            msgs.append(f"❌ No premium for {strike}{opt_type} — aborting strategy")
            _rollback(engine, placed, msgs)
            return False, msgs

        ok, msg = engine.place_order(
            underlying   = leg["underlying"],
            strike       = strike,
            opt_type     = opt_type,
            expiry       = expiry,
            action       = action,
            lots         = leg["lots"],
            premium      = premium,
            strategy_tag = leg["strategy_tag"],
            leg_tag      = leg["leg_tag"],
            iv           = iv if action == "SELL" else None,
        )
        msgs.append(msg)
        if ok:
            from options_paper_engine import _pos_key
            placed.append(_pos_key(leg["underlying"], strike, opt_type, expiry))
        else:
            _rollback(engine, placed, msgs)
            return False, msgs

    return True, msgs


def _rollback(engine, placed: list, msgs: list):
    """Roll back all placed legs on strategy failure."""
    for placed_key in placed:
        pos = engine.get_position(placed_key)
        if pos:
            engine.close_position(placed_key, pos["ltp"], reason="strategy_rollback")
            msgs.append(f"↩️ Rolled back: {placed_key}")
        else:
            log.warning(f"[STRAT] Rollback: position {placed_key} not found — may already be closed")


# ─── Strategy name resolver ───────────────────────────────────────────────────

STRATEGY_MAP = {
    "straddle":       straddle,
    "strangle":       strangle,
    "bull_call":      bull_call_spread,
    "bear_put":       bear_put_spread,
    "bull_put":       bull_put_spread,
    "bear_call":      bear_call_spread,
    "iron_condor":    iron_condor,
    "iron_butterfly": iron_butterfly,
}


def get_strategy_legs(
    name: str,
    underlying: str,
    spot: float,
    lots: int = 1,
) -> Optional[list[dict]]:
    fn = STRATEGY_MAP.get(name.lower())
    if fn is None:
        return None
    return fn(underlying, spot, lots)