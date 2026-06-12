"""
options_bot_commands.py
=======================
Paste these command handlers into bot_server.py _handle() function.
Also add at top of bot_server.py:

    from options_paper_engine import get_engine
    from options_monitor import start_monitor
    from options_strategies import get_strategy_legs, place_strategy, STRATEGY_MAP
    from fyers_data import get_fyers, get_quotes, get_option_chain

And in bot_server.py start() function, add after init():
    engine = get_engine()
    start_monitor(get_fyers, engine, lambda msg: _send(CHAT_ID, msg))

─────────────────────────────────────────────────────────────────────
COMMANDS:
  /optbuy    NIFTY 24500 CE 17JUN 1
  /optsell   NIFTY 24500 CE 17JUN 1
  /optclose  NIFTY_24500CE_17JUN
  /optsl     NIFTY_24500CE_17JUN 50
  /opttp     NIFTY_24500CE_17JUN 200
  /opttrail  NIFTY_24500CE_17JUN 30     — trailing SL distance ₹
  /optstatus — all open option positions
  /optpnl    — daily + total PnL
  /optlog    — last 10 closed trades
  /optreset  CONFIRM — reset paper account
  /optresume — resume trading after halt
  /strategy  NIFTY straddle 17JUN 1 [sell/buy]
  /stratpnl  STRATEGY_TAG — strategy P&L summary
  /optsettle NIFTY 17JUN 24500 — manual expiry settle
  /optprice  NIFTY 24500 CE 17JUN
  /opthelp   — command list
─────────────────────────────────────────────────────────────────────

NOTE: Expiry format now Tuesday-based (NSE changed NIFTY/BANKNIFTY expiry to Tuesday).
      Use format like 17JUN, 24JUN etc.
"""

from typing import Optional


# ─── Symbol helpers ───────────────────────────────────────────────────────────

def _build_fyers_symbol(underlying: str, strike: int, opt_type: str, expiry: str) -> Optional[str]:
    """Build Fyers option symbol string."""
    try:
        from datetime import datetime as _dt
        MONTHS = {
            "JAN": "1",  "FEB": "2",  "MAR": "3",  "APR": "4",
            "MAY": "5",  "JUN": "6",  "JUL": "7",  "AUG": "8",
            "SEP": "9",  "OCT": "10", "NOV": "11", "DEC": "12",
        }
        expiry   = expiry.upper()
        exchange = "BSE" if underlying == "SENSEX" else "NSE"
        day      = expiry[:2]
        mon      = expiry[2:]
        mm       = MONTHS.get(mon)
        if not mm:
            return None
        yy      = _dt.now().strftime("%y")
        day_int = int(day)
        if day_int >= 25:
            return f"{exchange}:{underlying}{yy}{mon}{strike}{opt_type}"
        else:
            return f"{exchange}:{underlying}{yy}{mm}{day}{strike}{opt_type}"
    except Exception:
        return None


def _fetch_option_ltp(fyers, underlying: str, strike: int, opt_type: str, expiry: str) -> Optional[float]:
    """Fetch live LTP for an option from Fyers."""
    try:
        sym = _build_fyers_symbol(underlying, strike, opt_type, expiry)
        if not sym:
            return None
        from fyers_data import get_quotes
        quotes = get_quotes(fyers, [sym])
        if quotes:
            return float(quotes[0]["v"]["lp"])
    except Exception:
        pass
    return None


def _fetch_spot(fyers, underlying: str) -> Optional[float]:
    """Fetch live spot price for underlying."""
    SPOT_SYMS = {
        "NIFTY":     "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "SENSEX":    "BSE:SENSEX-INDEX",
    }
    try:
        sym = SPOT_SYMS.get(underlying)
        if not sym:
            return None
        from fyers_data import get_quotes
        quotes = get_quotes(fyers, [sym])
        if quotes:
            return float(quotes[0]["v"]["lp"])
    except Exception:
        pass
    return None


def _fetch_iv(fyers, underlying: str) -> Optional[float]:
    """Fetch India VIX as IV proxy. Returns float or None."""
    try:
        from fyers_data import get_option_chain
        CHAIN_SYMS = {
            "NIFTY":     "NSE:NIFTY50-INDEX",
            "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
            "SENSEX":    "BSE:SENSEX-INDEX",
        }
        sym   = CHAIN_SYMS.get(underlying)
        chain = get_option_chain(fyers, sym, strike_count=1)
        if chain:
            vix = chain.get("indiavixData", {}).get("ltp")
            if vix:
                return float(vix)
    except Exception:
        pass
    return None


# ─── Format helpers ───────────────────────────────────────────────────────────

def _format_open_options(engine) -> str:
    positions = engine.get_open_positions()
    if not positions:
        return "📭 No open option positions."

    # Group by strategy
    by_strat = {}
    singles  = []
    for p in positions:
        tag = p.get("strategy_tag", "")
        if tag:
            by_strat.setdefault(tag, []).append(p)
        else:
            singles.append(p)

    lines = ["📊 <b>Open Option Positions</b>\n"]

    # Strategy groups
    for tag, legs in by_strat.items():
        strat_pnl = sum(l.get("unrealized_pnl", 0) for l in legs)
        pnl_str   = f"₹{strat_pnl:+.0f}"
        emoji     = "🟢" if strat_pnl >= 0 else "🔴"
        lines.append(f"📦 <b>[{tag}]</b>  Net PnL: {emoji}{pnl_str}")
        for p in legs:
            action = p["action"]
            sl_str = f"₹{p['sl']}" if p.get("sl") else "—"
            tp_str = f"₹{p['tp']}" if p.get("tp") else "—"
            trail  = f" 📐₹{p['trailing_sl']}" if p.get("trailing_sl") else ""
            lines.append(
                f"  {'📈' if action=='BUY' else '📉'} {p['strike']}{p['opt_type']} "
                f"{action} ₹{p['entry_premium']}→₹{p['ltp']} "
                f"PnL:₹{p.get('unrealized_pnl',0):+.0f} "
                f"SL:{sl_str} TP:{tp_str}{trail}\n"
                f"  🔑 <code>{p['key']}</code>"
            )
        lines.append("")

    # Singles
    for p in singles:
        action  = p["action"]
        pnl     = p.get("unrealized_pnl", 0)
        sl_str  = f"₹{p['sl']}" if p.get("sl") else "—"
        tp_str  = f"₹{p['tp']}" if p.get("tp") else "—"
        trail   = f" 📐₹{p['trailing_sl']}" if p.get("trailing_sl") else ""
        emoji   = "📈" if action == "BUY" else "📉"
        lines.append(
            f"{emoji} <b>{p['underlying']} {p['strike']}{p['opt_type']} {p['expiry']}</b>\n"
            f"   {action} | Entry: ₹{p['entry_premium']} | LTP: ₹{p['ltp']}\n"
            f"   Lots: {p['lots']} | PnL: <b>₹{pnl:+.0f}</b>\n"
            f"   SL: {sl_str} | TP: {tp_str}{trail}\n"
            f"   🔑 <code>{p['key']}</code>"
        )

    # Account summary footer
    s = engine.get_summary()
    lines.append(
        f"\n💰 Capital: ₹{s['capital']:,.0f} | Margin: {s['margin_pct']}% | "
        f"Open PnL: ₹{s['open_pnl']:+,.0f}"
    )
    if s.get("trading_halted"):
        lines.append(f"🛑 <b>TRADING HALTED</b>: {s['halt_reason']}")

    return "\n".join(lines)


# ─── Command handlers ─────────────────────────────────────────────────────────

def handle_options_commands(chat_id, text, _send_fn) -> bool:
    """
    Returns True if command was handled, False otherwise.
    Add at top of _handle() in bot_server.py:
        if handle_options_commands(chat_id, text, _send):
            return
    """
    from options_paper_engine import get_engine
    from options_strategies import get_strategy_legs, place_strategy, STRATEGY_MAP
    from fyers_data import get_fyers

    engine = get_engine()
    parts  = text.strip().split()
    cmd    = parts[0].lower() if parts else ""

    # ── /optbuy NIFTY 24500 CE 17JUN 1 ──────────────────────────────────────
    if cmd == "/optbuy":
        if len(parts) < 6:
            _send_fn(chat_id,
                "Usage: /optbuy UNDERLYING STRIKE TYPE EXPIRY LOTS\n"
                "Ex: /optbuy NIFTY 24500 CE 17JUN 1")
            return True
        underlying = parts[1].upper()
        try:
            strike = int(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ Strike must be a number."); return True
        opt_type = parts[3].upper()
        expiry   = parts[4].upper()
        lots     = int(parts[5]) if parts[5].isdigit() else 1

        fyers   = get_fyers()
        premium = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        if not premium:
            _send_fn(chat_id, f"❌ Could not fetch LTP for {underlying} {strike}{opt_type} {expiry}.")
            return True

        ok, msg = engine.place_order(underlying, strike, opt_type, expiry, "BUY", lots, premium)
        _send_fn(chat_id, msg)
        return True

    # ── /optsell NIFTY 24500 CE 17JUN 1 ─────────────────────────────────────
    elif cmd == "/optsell":
        if len(parts) < 6:
            _send_fn(chat_id,
                "Usage: /optsell UNDERLYING STRIKE TYPE EXPIRY LOTS\n"
                "Ex: /optsell NIFTY 24500 CE 17JUN 1")
            return True
        underlying = parts[1].upper()
        try:
            strike = int(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ Strike must be a number."); return True
        opt_type = parts[3].upper()
        expiry   = parts[4].upper()
        lots     = int(parts[5]) if parts[5].isdigit() else 1

        fyers   = get_fyers()
        premium = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        if not premium:
            _send_fn(chat_id, f"❌ Could not fetch LTP for {underlying} {strike}{opt_type} {expiry}.")
            return True

        iv = _fetch_iv(fyers, underlying)
        ok, msg = engine.place_order(
            underlying, strike, opt_type, expiry, "SELL", lots, premium, iv=iv
        )
        _send_fn(chat_id, msg)
        return True

    # ── /optclose KEY ─────────────────────────────────────────────────────────
    elif cmd == "/optclose":
        if len(parts) < 2:
            _send_fn(chat_id, "Usage: /optclose KEY\nGet key from /optstatus")
            return True
        key = parts[1]
        pos = engine.get_position(key)
        if not pos:
            _send_fn(chat_id, f"❌ No position: {key}"); return True

        fyers = get_fyers()
        ltp   = _fetch_option_ltp(fyers, pos["underlying"], pos["strike"], pos["opt_type"], pos["expiry"])
        if not ltp:
            ltp = pos["ltp"]
            _send_fn(chat_id, f"⚠️ Using last known LTP ₹{ltp} (live fetch failed)")

        ok, msg = engine.close_position(key, ltp, reason="manual")
        _send_fn(chat_id, msg)
        return True

    # ── /optsl KEY PREMIUM ────────────────────────────────────────────────────
    elif cmd == "/optsl":
        if len(parts) < 3:
            _send_fn(chat_id, "Usage: /optsl KEY SL_PREMIUM"); return True
        key = parts[1]
        try:
            sl = float(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ SL must be a number."); return True
        ok, msg = engine.set_sl(key, sl)
        _send_fn(chat_id, msg)
        return True

    # ── /opttp KEY PREMIUM ────────────────────────────────────────────────────
    elif cmd == "/opttp":
        if len(parts) < 3:
            _send_fn(chat_id, "Usage: /opttp KEY TP_PREMIUM"); return True
        key = parts[1]
        try:
            tp = float(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ TP must be a number."); return True
        ok, msg = engine.set_tp(key, tp)
        _send_fn(chat_id, msg)
        return True

    # ── /opttrail KEY DISTANCE ────────────────────────────────────────────────
    elif cmd == "/opttrail":
        if len(parts) < 3:
            _send_fn(chat_id,
                "Usage: /opttrail KEY TRAIL_DISTANCE\n"
                "Ex: /opttrail NIFTY_24500CE_17JUN 30\n"
                "Trailing SL moves up as premium rises. BUY positions only.")
            return True
        key = parts[1]
        try:
            dist = float(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ Distance must be a number."); return True
        ok, msg = engine.set_trailing_sl(key, dist)
        _send_fn(chat_id, msg)
        return True

    # ── /optstatus ────────────────────────────────────────────────────────────
    elif cmd == "/optstatus":
        _send_fn(chat_id, _format_open_options(engine))
        return True

    # ── /optpnl ───────────────────────────────────────────────────────────────
    elif cmd == "/optpnl":
        s       = engine.get_summary()
        day_pnl = engine.get_daily_pnl()
        halt_str = f"\n🛑 <b>HALTED</b>: {s['halt_reason']}" if s.get("trading_halted") else ""
        _send_fn(chat_id,
            f"💰 <b>Options Paper Account</b>\n\n"
            f"Capital: ₹{s['capital']:,.0f}\n"
            f"Margin Used: ₹{s['used_margin']:,.0f} ({s['margin_pct']}%)\n"
            f"Available: ₹{s['available']:,.0f}\n\n"
            f"Open PnL:       ₹{s['open_pnl']:+,.0f}\n"
            f"Today PnL:      ₹{day_pnl:+,.0f}\n"
            f"Total Realized: ₹{s['realized_pnl']:+,.0f}\n"
            f"Net PnL:        ₹{s['net_pnl']:+,.0f}\n\n"
            f"Open Positions: {s['open_positions']}"
            f"{halt_str}"
        )
        return True

    # ── /optlog [N] ───────────────────────────────────────────────────────────
    elif cmd == "/optlog":
        limit = 10
        if len(parts) > 1:
            try:
                limit = int(parts[1])
            except ValueError:
                pass
        log_entries = engine.get_trade_log(limit)
        if not log_entries:
            _send_fn(chat_id, "📭 No closed trades yet."); return True

        lines = [f"📜 <b>Last {len(log_entries)} Closed Option Trades</b>\n"]
        for t in reversed(log_entries):
            emoji  = "🟢" if t["pnl"] >= 0 else "🔴"
            strat  = f" [{t['strategy_tag']}]" if t.get("strategy_tag") else ""
            hold   = f" | Hold: {t.get('hold_minutes', '?')}m" if t.get("hold_minutes") else ""
            lines.append(
                f"{emoji} {t['underlying']} {t['strike']}{t['opt_type']} {t['expiry']}{strat}\n"
                f"   {t['action']} | ₹{t['entry_premium']} → ₹{t['exit_premium']}\n"
                f"   PnL: <b>₹{t['pnl']:+.0f}</b> | {t['result']} | {t['reason']}{hold}\n"
                f"   {t.get('date','')}"
            )
        _send_fn(chat_id, "\n\n".join(lines))
        return True

    # ── /strategy NIFTY straddle 17JUN 1 [sell/buy] ──────────────────────────
    elif cmd == "/strategy":
        if len(parts) < 5:
            avail = " | ".join(STRATEGY_MAP.keys())
            _send_fn(chat_id,
                f"Usage: /strategy UNDERLYING NAME EXPIRY LOTS [sell/buy]\n"
                f"Ex: /strategy NIFTY iron_condor 17JUN 1\n"
                f"Strategies: {avail}")
            return True

        underlying = parts[1].upper()
        strat_name = parts[2].lower()
        expiry     = parts[3].upper()
        lots       = int(parts[4]) if parts[4].isdigit() else 1
        action     = parts[5].upper() if len(parts) > 5 else "SELL"

        fyers = get_fyers()
        spot  = _fetch_spot(fyers, underlying)
        if not spot:
            _send_fn(chat_id, f"❌ Could not fetch spot for {underlying}"); return True

        legs = get_strategy_legs(strat_name, underlying, spot, lots)
        if legs is None:
            avail = " | ".join(STRATEGY_MAP.keys())
            _send_fn(chat_id, f"❌ Unknown strategy: {strat_name}\nAvailable: {avail}")
            return True

        # Override action for straddle/strangle
        if strat_name in ("straddle", "strangle"):
            from options_strategies import straddle, strangle
            fn   = straddle if strat_name == "straddle" else strangle
            legs = fn(underlying, spot, lots, action)

        # Fetch premiums
        premiums = {}
        missing  = []
        for leg in legs:
            ltp = _fetch_option_ltp(fyers, leg["underlying"], leg["strike"], leg["opt_type"], expiry)
            if ltp:
                premiums[(leg["strike"], leg["opt_type"])] = ltp
            else:
                missing.append(f"{leg['strike']}{leg['opt_type']}")

        if missing:
            _send_fn(chat_id, f"❌ Could not fetch LTP for: {', '.join(missing)}")
            return True

        # Fetch IV for sell validation
        iv = None
        has_sell = any(l["action"] == "SELL" for l in legs)
        if has_sell:
            iv = _fetch_iv(fyers, underlying)

        _send_fn(chat_id, f"⏳ Placing {strat_name.upper()} ({len(legs)} legs)...")
        ok, msgs = place_strategy(engine, legs, expiry, premiums, iv=iv)

        summary = f"{'✅' if ok else '❌'} <b>{strat_name.upper()}</b> {'placed' if ok else 'failed'}\n\n"
        summary += "\n\n".join(msgs)
        _send_fn(chat_id, summary)
        return True

    # ── /stratpnl STRATEGY_TAG ────────────────────────────────────────────────
    elif cmd == "/stratpnl":
        if len(parts) < 2:
            _send_fn(chat_id, "Usage: /stratpnl STRATEGY_TAG\nGet tag from /optstatus"); return True
        tag  = parts[1]
        data = engine.get_strategy_pnl(tag)
        emoji = "🟢" if data["net_pnl"] >= 0 else "🔴"
        _send_fn(chat_id,
            f"📊 <b>Strategy PnL: {tag}</b>\n\n"
            f"Open PnL:     ₹{data['open_pnl']:+,.0f}\n"
            f"Realized PnL: ₹{data['realized_pnl']:+,.0f}\n"
            f"Net PnL:      {emoji} ₹{data['net_pnl']:+,.0f}\n"
            f"Legs open:    {data['legs_open']}"
        )
        return True

    # ── /optresume ────────────────────────────────────────────────────────────
    elif cmd == "/optresume":
        engine.resume_trading()
        _send_fn(chat_id, "✅ Trading resumed.")
        return True

    # ── /optreset CONFIRM ─────────────────────────────────────────────────────
    elif cmd == "/optreset":
        if len(parts) > 1 and parts[1].upper() == "CONFIRM":
            msg = engine.reset()
            _send_fn(chat_id, msg)
        else:
            _send_fn(chat_id,
                "⚠️ This will wipe ALL option positions and reset capital to ₹5,00,000.\n"
                "Type /optreset CONFIRM to proceed.")
        return True

    # ── /optsettle NIFTY 17JUN 24500 ─────────────────────────────────────────
    elif cmd == "/optsettle":
        if len(parts) < 4:
            _send_fn(chat_id, "Usage: /optsettle UNDERLYING EXPIRY SPOT\nEx: /optsettle NIFTY 17JUN 24500")
            return True
        underlying = parts[1].upper()
        expiry     = parts[2].upper()
        try:
            spot = float(parts[3])
        except ValueError:
            _send_fn(chat_id, "❌ Spot must be a number."); return True

        msgs = engine.settle_expiry(expiry, spot)
        if not msgs:
            _send_fn(chat_id, f"No positions found for expiry {expiry}.")
        else:
            _send_fn(chat_id, "\n\n".join(msgs))
        return True

    # ── /optprice NIFTY 24500 CE 17JUN ───────────────────────────────────────
    elif cmd == "/optprice":
        if len(parts) < 5:
            _send_fn(chat_id,
                "Usage: /optprice UNDERLYING STRIKE TYPE EXPIRY\n"
                "Ex: /optprice NIFTY 24500 CE 17JUN")
            return True
        underlying = parts[1].upper()
        try:
            strike = int(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ Strike must be a number."); return True
        opt_type = parts[3].upper()
        expiry   = parts[4].upper()

        fyers = get_fyers()
        ltp   = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        spot  = _fetch_spot(fyers, underlying)

        if not ltp:
            _send_fn(chat_id,
                f"❌ Could not fetch price for {underlying} {strike}{opt_type} {expiry}.\n"
                f"Check symbol/expiry format.")
            return True

        spot_str = f"₹{spot:,.2f}" if spot else "N/A"
        sym      = _build_fyers_symbol(underlying, strike, opt_type, expiry) or "?"
        _send_fn(chat_id,
            f"📊 <b>{underlying} {strike} {opt_type} {expiry}</b>\n\n"
            f"Premium LTP: <b>₹{ltp:.2f}</b>\n"
            f"{underlying} Spot: {spot_str}\n"
            f"Symbol: <code>{sym}</code>"
        )
        return True

    # ── /opthelp ──────────────────────────────────────────────────────────────
    elif cmd == "/opthelp":
        _send_fn(chat_id,
            "🎯 <b>Options Paper Trading Commands</b>\n\n"
            "<b>Price Check</b>\n"
            "/optprice NIFTY 24500 CE 17JUN\n\n"
            "<b>Single Leg</b>\n"
            "/optbuy  NIFTY 24500 CE 17JUN 1\n"
            "/optsell NIFTY 24500 CE 17JUN 1\n\n"
            "<b>Manage</b>\n"
            "/optclose KEY          — close position\n"
            "/optsl KEY PREMIUM     — set stop loss\n"
            "/opttp KEY PREMIUM     — set target\n"
            "/opttrail KEY DIST     — set trailing SL (BUY only)\n\n"
            "<b>Multi-Leg Strategies</b>\n"
            "/strategy NIFTY straddle 17JUN 1\n"
            "/strategy NIFTY strangle 17JUN 1 sell\n"
            "/strategy NIFTY bull_call 17JUN 1\n"
            "/strategy NIFTY bear_put 17JUN 1\n"
            "/strategy NIFTY bull_put 17JUN 1\n"
            "/strategy NIFTY bear_call 17JUN 1\n"
            "/strategy NIFTY iron_condor 17JUN 1\n"
            "/strategy NIFTY iron_butterfly 17JUN 1\n"
            "/stratpnl STRATEGY_TAG — strategy P&L\n\n"
            "<b>Account</b>\n"
            "/optstatus  — all open positions\n"
            "/optpnl     — account summary\n"
            "/optlog     — trade history\n"
            "/optresume  — resume after halt\n"
            "/optreset   — reset account\n\n"
            "<b>Expiry</b>\n"
            "/optsettle NIFTY 17JUN 24500\n\n"
            "<i>Note: NIFTY/BANKNIFTY expiry = Tuesday (NSE changed from Thursday)</i>"
        )
        return True

    return False