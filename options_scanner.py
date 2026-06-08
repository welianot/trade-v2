"""
options_scanner.py
==================
Options scanner: NIFTY, BANKNIFTY, SENSEX.

Strategy:
  PRIMARY  — 5m candle CLOSE above PDH → BULLISH
             5m candle CLOSE below PDL → BEARISH
             Inside range             → SIDEWAYS
  SECONDARY (confirms / adjusts):
    PCR     — Put-Call Ratio (from OI totals)
    OI Wall — Max pain / heavy OI strike walls from chain
    Spread  — Call-Put OI spread at ATM±2 strikes
    VIX     — vol context (buy vs sell bias)

Trade outputs:
  BULLISH  → BUY 1-OTM CE  (1:2 RR, lots auto-sized to risk)
  BEARISH  → BUY 1-OTM PE  (1:2 RR, lots auto-sized to risk)
  SIDEWAYS + VIX>18  → SELL ATM Straddle (paper only)
  SIDEWAYS + VIX<=18 → SELL ATM Strangle (paper only, wider wings)
  Weak signal        → NO TRADE

Returns:
  scan_options(fyers)         → formatted HTML string (for /options command)
  get_signal(fyers, index)    → dict with trade details (for auto-trader)
"""

import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────

SYMBOLS = {
    "NIFTY":     "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "SENSEX":    "BSE:SENSEX-INDEX",
}

STRIKE_STEP   = {"NIFTY": 50,    "BANKNIFTY": 100,   "SENSEX": 100}
LOT_SIZE      = {"NIFTY": 50,    "BANKNIFTY": 30,    "SENSEX": 20}
MARGIN_PER_LOT= {"NIFTY": 80000, "BANKNIFTY": 50000, "SENSEX": 60000}

# Bias thresholds
PCR_BULL      = 1.3    # PCR above → bullish sentiment
PCR_BEAR      = 0.75   # PCR below → bearish sentiment
VIX_SELL_HIGH = 18.0   # VIX above → straddle selling preferred
VIX_SELL_LOW  = 12.0   # VIX below → strangle (wider, less premium risk)

# Risk / sizing
RISK_INR      = 5000   # max risk per trade in ₹
RR_RATIO      = 2.0    # 1:2 risk reward
SL_PCT        = 0.40   # SL = 40% of premium paid (buyer trades)
OTM_STRIKES   = 1      # how many strikes OTM to buy


def _round_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)


def _calc_buy_lots(premium: float, lot_size: int) -> int:
    """Auto-size lots based on RISK_INR at 1:2 RR. Min 1 lot."""
    if premium <= 0:
        return 1
    cost_per_lot = premium * lot_size
    lots = int(RISK_INR / cost_per_lot)
    return max(1, lots)


def _find_option(options: list, opt_type: str, target_strike: int) -> Optional[dict]:
    """Find most liquid option near target_strike by OI then proximity."""
    candidates = [
        o for o in options
        if o.get("option_type") == opt_type
        and o.get("strike_price", -1) > 0
        and float(o.get("ltp") or 0) > 0.5
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: (abs(x["strike_price"] - target_strike), -int(x.get("oi") or 0)))
    return candidates[0]


def _get_pdh_pdl_5m(fyers, fyers_sym: str) -> tuple:
    """
    Returns (pdh, pdl, last_5m_close, last_5m_candle).
    Checks that 5m candle actually CLOSES above/below level (not just wicks).
    """
    try:
        from fyers_data import get_history
        import concurrent.futures

        def fetch_daily():
            return get_history(fyers, fyers_sym, resolution="D", days_back=5)

        def fetch_5m():
            return get_history(fyers, fyers_sym, resolution="5", days_back=1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_daily = ex.submit(fetch_daily)
            f_5m    = ex.submit(fetch_5m)
            try:
                daily = f_daily.result(timeout=15)
            except concurrent.futures.TimeoutError:
                log.warning(f"Daily candle fetch timed out: {fyers_sym}")
                return None, None, None, None
            try:
                m5 = f_5m.result(timeout=15)
            except concurrent.futures.TimeoutError:
                log.warning(f"5m candle fetch timed out: {fyers_sym}")
                m5 = None

        if not daily or len(daily) < 2:
            return None, None, None, None
        prev = daily[-2]
        pdh = float(prev[2])   # high
        pdl = float(prev[3])   # low

        if not m5 or len(m5) < 2:
            return pdh, pdl, None, None
        last = m5[-1]
        last_close = float(last[4])   # close price
        return pdh, pdl, last_close, last
    except Exception as e:
        log.warning(f"PDH/PDL fetch failed {fyers_sym}: {e}")
        return None, None, None, None


def _analyse_oi_chain(options: list, atm: int, step: int) -> dict:
    """
    Analyse option chain for:
    - Max pain strike (weighted OI)
    - OI walls (heavy resistance/support)
    - CE/PE OI spread at ATM±2
    - PCR by strike range
    """
    result = {
        "max_pain":    None,
        "ce_wall":     None,   # strike with max CE OI (resistance)
        "pe_wall":     None,   # strike with max PE OI (support)
        "atm_spread":  None,   # (ce_oi - pe_oi) at ATM, +ve = more calls = bearish wall
        "pcr_atm":     None,   # PCR for ATM±3 strikes only
        "oi_bias":     "neutral",
    }
    if not options:
        return result

    # Build maps
    ce_map = {o["strike_price"]: int(o.get("oi") or 0)
              for o in options if o.get("option_type") == "CE"}
    pe_map = {o["strike_price"]: int(o.get("oi") or 0)
              for o in options if o.get("option_type") == "PE"}

    if ce_map:
        result["ce_wall"] = max(ce_map, key=ce_map.get)
    if pe_map:
        result["pe_wall"] = max(pe_map, key=pe_map.get)

    # ATM spread (CE OI - PE OI) at ATM strike
    atm_ce = ce_map.get(atm, 0)
    atm_pe = pe_map.get(atm, 0)
    result["atm_spread"] = atm_ce - atm_pe

    # PCR for ATM±3 range
    strikes_near = [atm + i * step for i in range(-3, 4)]
    near_ce_oi = sum(ce_map.get(s, 0) for s in strikes_near)
    near_pe_oi = sum(pe_map.get(s, 0) for s in strikes_near)
    if near_ce_oi > 0:
        result["pcr_atm"] = round(near_pe_oi / near_ce_oi, 3)

    # Max pain — strike where total OI pain is minimum
    all_strikes = sorted(set(list(ce_map.keys()) + list(pe_map.keys())))
    min_pain = None
    min_pain_strike = None
    for s in all_strikes:
        pain = sum(max(0, s - k) * ce_map.get(k, 0) for k in all_strikes) + \
               sum(max(0, k - s) * pe_map.get(k, 0) for k in all_strikes)
        if min_pain is None or pain < min_pain:
            min_pain = pain
            min_pain_strike = s
    result["max_pain"] = min_pain_strike

    # OI bias
    if result["pcr_atm"] is not None:
        if result["pcr_atm"] > PCR_BULL:
            result["oi_bias"] = "bullish"
        elif result["pcr_atm"] < PCR_BEAR:
            result["oi_bias"] = "bearish"

    return result


def _determine_bias(vix, pcr_total, pdh, pdl, last_close, oi_analysis) -> tuple:
    """
    Returns (bias, strength, reasons).
    bias: 'bullish' | 'bearish' | 'sideways'
    strength: 'strong' | 'moderate' | 'weak'
    """
    votes = {"bullish": 0, "bearish": 0, "sideways": 0}
    reasons = []
    primary_bias = None

    # ── PRIMARY: 5m close above/below PDH/PDL ──────────────────────────────
    if pdh and pdl and last_close:
        if last_close > pdh:
            primary_bias = "bullish"
            votes["bullish"] += 3   # primary gets 3 votes
            reasons.append(
                f"✅ <b>5m CLOSE {last_close:.1f} > PDH {pdh:.1f}</b> → Bullish breakout"
            )
        elif last_close < pdl:
            primary_bias = "bearish"
            votes["bearish"] += 3
            reasons.append(
                f"✅ <b>5m CLOSE {last_close:.1f} < PDL {pdl:.1f}</b> → Bearish breakdown"
            )
        else:
            votes["sideways"] += 2
            reasons.append(
                f"⏸ 5m close {last_close:.1f} inside PDH {pdh:.1f} / PDL {pdl:.1f} → Ranging"
            )
    else:
        reasons.append("⚠️ PDH/PDL data unavailable")

    # ── SECONDARY 1: Total PCR ──────────────────────────────────────────────
    if pcr_total is not None:
        if pcr_total > PCR_BULL:
            votes["bullish"] += 1
            reasons.append(f"📊 PCR (total) {pcr_total:.2f} > {PCR_BULL} → Bullish OI sentiment")
        elif pcr_total < PCR_BEAR:
            votes["bearish"] += 1
            reasons.append(f"📊 PCR (total) {pcr_total:.2f} < {PCR_BEAR} → Bearish OI sentiment")
        else:
            votes["sideways"] += 1
            reasons.append(f"📊 PCR (total) {pcr_total:.2f} → Neutral")

    # ── SECONDARY 2: ATM PCR (near-strike OI) ──────────────────────────────
    pcr_atm = oi_analysis.get("pcr_atm")
    if pcr_atm is not None:
        if pcr_atm > PCR_BULL:
            votes["bullish"] += 1
            reasons.append(f"📊 PCR (ATM±3) {pcr_atm:.2f} → Bullish near-term OI")
        elif pcr_atm < PCR_BEAR:
            votes["bearish"] += 1
            reasons.append(f"📊 PCR (ATM±3) {pcr_atm:.2f} → Bearish near-term OI")

    # ── SECONDARY 3: OI Walls ──────────────────────────────────────────────
    ce_wall = oi_analysis.get("ce_wall")
    pe_wall = oi_analysis.get("pe_wall")
    if ce_wall and pe_wall and last_close:
        if last_close > ce_wall:
            votes["bullish"] += 1
            reasons.append(f"🏋 Spot {last_close:.0f} broke CE wall at {ce_wall} → Bullish momentum")
        elif last_close < pe_wall:
            votes["bearish"] += 1
            reasons.append(f"🏋 Spot {last_close:.0f} broke PE wall at {pe_wall} → Bearish momentum")
        else:
            reasons.append(f"🏋 CE wall: {ce_wall}  |  PE wall: {pe_wall}  (spot between walls)")

    # ── SECONDARY 4: Max Pain ──────────────────────────────────────────────
    max_pain = oi_analysis.get("max_pain")
    if max_pain and last_close:
        diff = last_close - max_pain
        if abs(diff) < 100:
            reasons.append(f"🎯 Max pain: {max_pain} (spot near → sideways pull)")
            votes["sideways"] += 1
        elif diff > 0:
            reasons.append(f"🎯 Max pain: {max_pain} (spot above → sellers may defend)")
        else:
            reasons.append(f"🎯 Max pain: {max_pain} (spot below → buyers may push)")

    # ── VIX context (no vote, only context) ────────────────────────────────
    if vix is not None:
        if vix > 20:
            reasons.append(f"🌡 VIX {vix:.1f} — High volatility: prefer option selling")
        elif vix > VIX_SELL_HIGH:
            reasons.append(f"🌡 VIX {vix:.1f} — Elevated: straddle selling viable")
        elif vix < VIX_SELL_LOW:
            reasons.append(f"🌡 VIX {vix:.1f} — Low vol: buying options is cheaper")
        else:
            reasons.append(f"🌡 VIX {vix:.1f} — Moderate")

    # ── Resolve bias ───────────────────────────────────────────────────────
    max_v = max(votes.values())
    top   = [k for k, v in votes.items() if v == max_v]

    if len(top) == 1:
        bias = top[0]
    elif primary_bias and primary_bias in top:
        bias = primary_bias   # primary breakout wins tie
    else:
        bias = "sideways"

    # Strength
    total_votes = sum(votes.values())
    bias_votes  = votes[bias]
    strength = "strong" if bias_votes >= 4 else "moderate" if bias_votes >= 2 else "weak"

    return bias, strength, reasons


# ─── SIGNAL BUILDER ──────────────────────────────────────────────────────────

def get_signal(fyers, index: str) -> Optional[dict]:
    """
    Returns a trade signal dict or None.
    Used by auto-trader loop.

    Signal dict keys:
      index, bias, strength, action, underlying, strike, opt_type,
      expiry, lots, premium, sl_premium, tp_premium, reasons,
      atm, spot, vix, pcr
    """
    from fyers_data import get_quotes, get_option_chain

    fyers_sym = SYMBOLS.get(index.upper())
    if not fyers_sym:
        return None

    step     = STRIKE_STEP[index]
    lot_size = LOT_SIZE[index]

    # Spot
    try:
        quotes = get_quotes(fyers, [fyers_sym])
        if not quotes:
            return None
        spot = float(quotes[0]["v"]["lp"])
    except Exception:
        return None

    atm = _round_strike(spot, step)

    # PDH/PDL
    pdh, pdl, last_close, _ = _get_pdh_pdl_5m(fyers, fyers_sym)

    # Chain
    options    = []
    pcr_total  = None
    vix        = None
    expiry_str = ""
    try:
        chain = get_option_chain(fyers, fyers_sym, strike_count=8)
        if chain:
            call_oi = float(chain.get("callOi") or 0)
            put_oi  = float(chain.get("putOi")  or 0)
            if call_oi > 10000:
                pcr_total = round(put_oi / call_oi, 3)
            all_items = chain.get("optionsChain", [])
            options   = [x for x in all_items if x.get("strike_price", -1) > 0]
            vix_raw   = chain.get("indiavixData", {}).get("ltp")
            if vix_raw:
                vix = float(vix_raw)
            exp_list = chain.get("expiryData", [])
            if exp_list:
                ts = exp_list[0].get("expiry", "")
                if ts:
                    try:
                        expiry_str = datetime.fromtimestamp(int(ts)).strftime("%d%b").upper()
                    except Exception:
                        expiry_str = ""
    except Exception as e:
        log.warning(f"Chain fetch {index}: {e}")

    oi_analysis = _analyse_oi_chain(options, atm, step)
    bias, strength, reasons = _determine_bias(vix, pcr_total, pdh, pdl, last_close, oi_analysis)

    if strength == "weak":
        return None

    signal = {
        "index":      index,
        "bias":       bias,
        "strength":   strength,
        "spot":       spot,
        "atm":        atm,
        "pdh":        pdh,
        "pdl":        pdl,
        "last_close": last_close,
        "vix":        vix,
        "pcr":        pcr_total,
        "pcr_atm":    oi_analysis.get("pcr_atm"),
        "ce_wall":    oi_analysis.get("ce_wall"),
        "pe_wall":    oi_analysis.get("pe_wall"),
        "max_pain":   oi_analysis.get("max_pain"),
        "expiry":     expiry_str,
        "reasons":    reasons,
        "action":     None,
        "underlying": index,
        "strike":     None,
        "opt_type":   None,
        "lots":       None,
        "premium":    None,
        "sl_premium": None,
        "tp_premium": None,
        "strategy":   None,
    }

    if bias == "bullish":
        target_strike = atm + step * OTM_STRIKES
        opt = _find_option(options, "CE", target_strike)
        if opt:
            premium    = float(opt["ltp"])
            lots       = _calc_buy_lots(premium, lot_size)
            sl_premium = round(premium * (1 - SL_PCT), 2)
            tp_premium = round(premium * (1 + SL_PCT * RR_RATIO), 2)
            signal.update({
                "action":     "BUY",
                "strike":     opt["strike_price"],
                "opt_type":   "CE",
                "lots":       lots,
                "premium":    premium,
                "sl_premium": sl_premium,
                "tp_premium": tp_premium,
                "strategy":   "buy_ce",
                "oi":         int(opt.get("oi") or 0),
            })

    elif bias == "bearish":
        target_strike = atm - step * OTM_STRIKES
        opt = _find_option(options, "PE", target_strike)
        if opt:
            premium    = float(opt["ltp"])
            lots       = _calc_buy_lots(premium, lot_size)
            sl_premium = round(premium * (1 - SL_PCT), 2)
            tp_premium = round(premium * (1 + SL_PCT * RR_RATIO), 2)
            signal.update({
                "action":     "BUY",
                "strike":     opt["strike_price"],
                "opt_type":   "PE",
                "lots":       lots,
                "premium":    float(opt["ltp"]),
                "sl_premium": sl_premium,
                "tp_premium": tp_premium,
                "strategy":   "buy_pe",
                "oi":         int(opt.get("oi") or 0),
            })

    elif bias == "sideways":
        ce_opt = _find_option(options, "CE", atm)
        pe_opt = _find_option(options, "PE", atm)
        if vix and vix > VIX_SELL_HIGH and ce_opt and pe_opt:
            # Straddle
            total_premium = round(float(ce_opt["ltp"]) + float(pe_opt["ltp"]), 2)
            signal.update({
                "action":      "SELL",
                "strategy":    "straddle",
                "strike":      atm,
                "ce_premium":  float(ce_opt["ltp"]),
                "pe_premium":  float(pe_opt["ltp"]),
                "premium":     total_premium,
                "lots":        1,
                "sl_pts":      step * 2,  # SL if spot moves > 2 steps
            })
        elif ce_opt and pe_opt:
            # Strangle — 1 OTM each side
            ce_otm = _find_option(options, "CE", atm + step)
            pe_otm = _find_option(options, "PE", atm - step)
            if ce_otm and pe_otm:
                total_premium = round(float(ce_otm["ltp"]) + float(pe_otm["ltp"]), 2)
                signal.update({
                    "action":      "SELL",
                    "strategy":    "strangle",
                    "ce_strike":   ce_otm["strike_price"],
                    "pe_strike":   pe_otm["strike_price"],
                    "ce_premium":  float(ce_otm["ltp"]),
                    "pe_premium":  float(pe_otm["ltp"]),
                    "premium":     total_premium,
                    "lots":        1,
                    "sl_pts":      step * 3,
                })

    return signal


# ─── SCAN (used by /options command) ─────────────────────────────────────────

def scan_options(fyers) -> str:
    """Full formatted scan report for /options Telegram command."""
    import concurrent.futures
    from fyers_data import get_quotes, get_option_chain

    now_str = datetime.now().strftime("%d %b %Y  %H:%M")
    lines   = [f"<b>🔍 OPTIONS SCAN</b>  |  {now_str}\n"]

    # VIX once from Nifty chain (with timeout)
    vix = None
    try:
        def _get_vix():
            return get_option_chain(fyers, "NSE:NIFTY50-INDEX", strike_count=1)
        with concurrent.futures.ThreadPoolExecutor() as ex:
            f = ex.submit(_get_vix)
            try:
                nifty_chain_vix = f.result(timeout=12)
                if nifty_chain_vix:
                    vix_raw = nifty_chain_vix.get("indiavixData", {}).get("ltp")
                    if vix_raw:
                        vix = float(vix_raw)
            except concurrent.futures.TimeoutError:
                log.warning("VIX fetch timed out")
    except Exception as e:
        log.warning(f"VIX fetch: {e}")

    def _scan_index(index):
        """Scan a single index and return formatted lines."""
        fyers_sym = SYMBOLS[index]
        step     = STRIKE_STEP[index]
        lot_size = LOT_SIZE[index]
        result   = []

        result.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        result.append(f"<b>📌 {index}</b>")

        # Spot
        try:
            quotes = get_quotes(fyers, [fyers_sym])
            if not quotes:
                result.append("  ❌ Quote fetch failed\n")
                return result
            spot = float(quotes[0]["v"]["lp"])
        except Exception as e:
            result.append(f"  ❌ Quote error: {e}\n")
            return result

        atm = _round_strike(spot, step)
        result.append(f"  Spot: <b>{spot:.1f}</b>  |  ATM: {atm}")

        # PDH/PDL
        pdh, pdl, last_close, _ = _get_pdh_pdl_5m(fyers, fyers_sym)
        if pdh and pdl:
            breakout = ""
            if last_close:
                if last_close > pdh:
                    breakout = " 🔼 ABOVE PDH"
                elif last_close < pdl:
                    breakout = " 🔽 BELOW PDL"
            result.append(f"  PDH: {pdh:.1f}  |  PDL: {pdl:.1f}  |  Last 5m close: {last_close or 'N/A'}{breakout}")

        # Chain
        options    = []
        pcr_total  = None
        expiry_str = ""
        try:
            chain = get_option_chain(fyers, fyers_sym, strike_count=8)
            if chain:
                call_oi   = float(chain.get("callOi") or 0)
                put_oi    = float(chain.get("putOi")  or 0)
                if call_oi > 10000:
                    pcr_total = round(put_oi / call_oi, 3)
                all_items = chain.get("optionsChain", [])
                options   = [x for x in all_items if x.get("strike_price", -1) > 0]
                exp_list  = chain.get("expiryData", [])
                if exp_list:
                    ts = exp_list[0].get("expiry", "")
                    if ts:
                        try:
                            expiry_str = datetime.fromtimestamp(int(ts)).strftime("%d %b")
                            result.append(f"  Nearest expiry: {expiry_str}")
                        except Exception:
                            pass
        except Exception as e:
            log.warning(f"Chain fetch {index}: {e}")

        oi_analysis = _analyse_oi_chain(options, atm, step)
        bias, strength, reasons = _determine_bias(vix, pcr_total, pdh, pdl, last_close, oi_analysis)

        # OI summary
        if pcr_total is not None:
            result.append(f"  PCR (total): <b>{pcr_total:.2f}</b>  |  PCR (ATM±3): {oi_analysis.get('pcr_atm') or 'N/A'}")
        if oi_analysis.get("ce_wall") or oi_analysis.get("pe_wall"):
            result.append(
                f"  CE Wall (resistance): {oi_analysis.get('ce_wall') or 'N/A'}  |"
                f"  PE Wall (support): {oi_analysis.get('pe_wall') or 'N/A'}"
            )
        if oi_analysis.get("max_pain"):
            result.append(f"  Max Pain: {oi_analysis['max_pain']}")

        # Bias
        bias_emoji = {"bullish": "🟢", "bearish": "🔴", "sideways": "🟡"}.get(bias, "⚪")
        result.append(f"\n  {bias_emoji} <b>Bias: {bias.upper()}</b>  [{strength}]")
        for r in reasons:
            result.append(f"  {r}")
        result.append("")

        # Trade suggestion
        if bias == "bullish":
            target = atm + step * OTM_STRIKES
            opt = _find_option(options, "CE", target)
            if opt:
                ltp  = float(opt["ltp"])
                lots = _calc_buy_lots(ltp, lot_size)
                cost = round(ltp * lot_size * lots)
                sl_p = round(ltp * (1 - SL_PCT), 2)
                tp_p = round(ltp * (1 + SL_PCT * RR_RATIO), 2)
                result.append(f"  📈 <b>BUY CE  — {opt['strike_price']}CE</b>")
                result.append(f"  Premium: ₹{ltp}  |  OI: {int(opt.get('oi') or 0):,}")
                result.append(f"  Lots: {lots} × {lot_size} = {lots*lot_size} qty  |  Cost: ₹{cost}")
                result.append(f"  SL: ₹{sl_p} (−{int(SL_PCT*100)}%)  |  TP: ₹{tp_p} (+{int(SL_PCT*RR_RATIO*100)}%)  [1:{RR_RATIO:.0f} RR]")
            else:
                result.append("  📈 BUY CE — no liquid strike found")

        elif bias == "bearish":
            target = atm - step * OTM_STRIKES
            opt = _find_option(options, "PE", target)
            if opt:
                ltp  = float(opt["ltp"])
                lots = _calc_buy_lots(ltp, lot_size)
                cost = round(ltp * lot_size * lots)
                sl_p = round(ltp * (1 - SL_PCT), 2)
                tp_p = round(ltp * (1 + SL_PCT * RR_RATIO), 2)
                result.append(f"  📉 <b>BUY PE  — {opt['strike_price']}PE</b>")
                result.append(f"  Premium: ₹{ltp}  |  OI: {int(opt.get('oi') or 0):,}")
                result.append(f"  Lots: {lots} × {lot_size} = {lots*lot_size} qty  |  Cost: ₹{cost}")
                result.append(f"  SL: ₹{sl_p} (−{int(SL_PCT*100)}%)  |  TP: ₹{tp_p} (+{int(SL_PCT*RR_RATIO*100)}%)  [1:{RR_RATIO:.0f} RR]")
            else:
                result.append("  📉 BUY PE — no liquid strike found")

        elif bias == "sideways":
            if vix and vix > VIX_SELL_HIGH:
                ce_opt = _find_option(options, "CE", atm)
                pe_opt = _find_option(options, "PE", atm)
                if ce_opt and pe_opt:
                    total = round(float(ce_opt["ltp"]) + float(pe_opt["ltp"]), 1)
                    result.append(f"  🔀 <b>SELL STRADDLE — ATM {atm}</b>")
                    result.append(f"  SELL {atm}CE @ ₹{ce_opt['ltp']}  +  SELL {atm}PE @ ₹{pe_opt['ltp']}")
                    result.append(f"  Total premium collected: ₹{total}/lot")
                    result.append(f"  SL: exit if spot moves >{step*2} pts from {atm}")
                else:
                    result.append("  🔀 SELL STRADDLE — chain data missing")
            else:
                ce_otm = _find_option(options, "CE", atm + step)
                pe_otm = _find_option(options, "PE", atm - step)
                if ce_otm and pe_otm:
                    total = round(float(ce_otm["ltp"]) + float(pe_otm["ltp"]), 1)
                    result.append(f"  🔀 <b>SELL STRANGLE</b>")
                    result.append(f"  SELL {ce_otm['strike_price']}CE @ ₹{ce_otm['ltp']}  +  SELL {pe_otm['strike_price']}PE @ ₹{pe_otm['ltp']}")
                    result.append(f"  Total premium collected: ₹{total}/lot")
                    result.append(f"  SL: exit if spot moves >{step*3} pts from ATM {atm}")
                else:
                    vix_s = f"{vix:.1f}" if vix else "N/A"
                    result.append(f"  ⏸ <b>NO TRADE</b>  (VIX {vix_s}, sideways — wait for breakout)")
        else:
            result.append("  ❓ Insufficient data")

        result.append("")
        return result

    # Run all 3 indices in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_scan_index, idx): idx for idx in SYMBOLS}
        for future in concurrent.futures.as_completed(futures, timeout=35):
            try:
                lines.extend(future.result())
            except Exception as e:
                idx = futures[future]
                lines.append(f"  ❌ {idx} scan error: {e}\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🌡 India VIX: <b>{vix:.1f}</b>" if vix else "🌡 India VIX: N/A")
    lines.append("⚠️ <i>Paper trade only. Verify before any real trades.</i>")

    return "\n".join(lines)


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from fyers_data import get_fyers
    fyers = get_fyers()
    if not fyers:
        print("Fyers auth failed.")
    else:
        print("Running scan...\n")
        result = scan_options(fyers)
        import re
        clean = re.sub(r"<[^>]+>", "", result)
        print(clean)
