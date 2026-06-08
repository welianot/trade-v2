"""
options_strategies.py
=====================
Multi-leg strategy builders. Each returns list of legs to place.
Engine places all legs atomically.

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


def _atm(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def _otm_ce(spot: float, step: int, n: int = 1) -> int:
    return _atm(spot, step) + step * n


def _otm_pe(spot: float, step: int, n: int = 1) -> int:
    return _atm(spot, step) - step * n


# Each strategy returns list of leg dicts:
# {underlying, strike, opt_type, action, lots, leg_tag}

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


def strangle(underlying: str, spot: float, lots: int = 1, action: str = "SELL",
             ce_offset: int = 2, pe_offset: int = 2) -> list[dict]:
    """Sell/Buy OTM CE + OTM PE."""
    step   = STRIKE_STEP[underlying]
    ce_str = _otm_ce(spot, step, ce_offset)
    pe_str = _otm_pe(spot, step, pe_offset)
    tag    = f"strangle_{ce_str}_{pe_str}_{action.lower()}"
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


def iron_condor(underlying: str, spot: float, lots: int = 1,
                wing_offset: int = 2) -> list[dict]:
    """
    Bull put spread (below) + Bear call spread (above).
    4 legs. Neutral strategy. Max profit in range.
    """
    step = STRIKE_STEP[underlying]
    atm  = _atm(spot, step)

    # Call side: sell ATM+1, buy ATM+wing
    short_ce = atm + step
    long_ce  = atm + step * wing_offset

    # Put side: sell ATM-1, buy ATM-wing
    short_pe = atm - step
    long_pe  = atm - step * wing_offset

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


def iron_butterfly(underlying: str, spot: float, lots: int = 1,
                   wing_offset: int = 2) -> list[dict]:
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

def place_strategy(engine, legs: list[dict], expiry: str, premiums: dict) -> tuple[bool, list[str]]:
    """
    Place all legs of a strategy atomically.
    premiums = {(strike, opt_type): ltp}
    Returns (all_ok, messages)
    """
    msgs   = []
    placed = []

    for leg in legs:
        strike   = leg["strike"]
        opt_type = leg["opt_type"]
        premium  = premiums.get((strike, opt_type))

        if premium is None or premium <= 0:
            msgs.append(f"❌ No premium for {strike}{opt_type} — aborting strategy")
            # Rollback placed legs
            for placed_key in placed:
                pos = engine.get_position(placed_key)
                if pos:
                    ltp = pos["ltp"]
                    engine.close_position(placed_key, ltp, reason="strategy_rollback")
            return False, msgs

        ok, msg = engine.place_order(
            underlying   = leg["underlying"],
            strike       = strike,
            opt_type     = opt_type,
            expiry       = expiry,
            action       = leg["action"],
            lots         = leg["lots"],
            premium      = premium,
            strategy_tag = leg["strategy_tag"],
            leg_tag      = leg["leg_tag"],
        )
        msgs.append(msg)
        if ok:
            from options_paper_engine import _pos_key
            placed.append(_pos_key(leg["underlying"], strike, opt_type, expiry))
        else:
            # Rollback
            for placed_key in placed:
                pos = engine.get_position(placed_key)
                if pos:
                    engine.close_position(placed_key, pos["ltp"], reason="strategy_rollback")
            return False, msgs

    return True, msgs


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

def get_strategy_legs(name: str, underlying: str, spot: float, lots: int = 1) -> Optional[list[dict]]:
    fn = STRATEGY_MAP.get(name.lower())
    if fn is None:
        return None
    return fn(underlying, spot, lots)