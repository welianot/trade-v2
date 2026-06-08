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
COMMANDS ADDED:
  /optbuy    NIFTY 24500 CE 26JUN 1
  /optsell   NIFTY 24500 CE 26JUN 1
  /optclose  NIFTY_24500CE_26JUN
  /optsl     NIFTY_24500CE_26JUN 50
  /opttp     NIFTY_24500CE_26JUN 200
  /optstatus — all open option positions
  /optpnl    — daily + total PnL
  /optlog    — last 10 closed trades
  /optreset  — reset paper account (with confirm)
  /strategy  NIFTY straddle 26JUN 1 [sell/buy]
  /optsettle NIFTY 26JUN 24500 — manual expiry settle
─────────────────────────────────────────────────────────────────────
"""

# ─── Paste these imports at top of bot_server.py ─────────────────────────────
# from options_paper_engine import get_engine
# from options_monitor import start_monitor
# from options_strategies import get_strategy_legs, place_strategy, STRATEGY_MAP
# from fyers_data import get_fyers, get_quotes, get_option_chain


# ─── Helper: fetch LTP for a single option ───────────────────────────────────

def _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry):
    """Fetch live LTP for an option from Fyers."""
    try:
        exchange = "BSE" if underlying == "SENSEX" else "NSE"
        sym      = f"{exchange}:{underlying}{expiry.upper()}{strike}{opt_type}"
        from fyers_data import get_quotes
        quotes = get_quotes(fyers, [sym])
        if quotes:
            return float(quotes[0]["v"]["lp"])
    except Exception as e:
        pass
    return None


def _fetch_spot(fyers, underlying):
    """Fetch live spot price for underlying."""
    SPOT_SYMS = {
        "NIFTY":     "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "SENSEX":    "BSE:SENSEX-INDEX",
    }
    try:
        sym    = SPOT_SYMS.get(underlying)
        if not sym:
            return None
        from fyers_data import get_quotes
        quotes = get_quotes(fyers, [sym])
        if quotes:
            return float(quotes[0]["v"]["lp"])
    except Exception:
        pass
    return None


# ─── Format open positions ────────────────────────────────────────────────────

def _format_open_options(engine) -> str:
    positions = engine.get_open_positions()
    if not positions:
        return "📭 No open option positions."

    lines = ["📊 <b>Open Option Positions</b>\n"]
    for p in positions:
        action  = p["action"]
        pnl     = p.get("unrealized_pnl", 0)
        emoji   = "📈" if action == "BUY" else "📉"
        strat   = f" [{p['strategy_tag']}]" if p.get("strategy_tag") else ""
        sl_str  = f"₹{p['sl']}" if p.get("sl") else "—"
        tp_str  = f"₹{p['tp']}" if p.get("tp") else "—"
        pnl_str = f"₹{pnl:+.0f}"

        lines.append(
            f"{emoji} <b>{p['underlying']} {p['strike']}{p['opt_type']} {p['expiry']}</b>{strat}\n"
            f"   {action} | Entry: ₹{p['entry_premium']} | LTP: ₹{p['ltp']}\n"
            f"   Lots: {p['lots']} | Qty: {p['qty']} | PnL: <b>{pnl_str}</b>\n"
            f"   SL: {sl_str} | TP: {tp_str}\n"
            f"   🔑 Key: <code>{p['key']}</code>"
        )
    return "\n\n".join(lines)


# ─── Command handlers (add inside _handle() in bot_server.py) ────────────────

def handle_options_commands(chat_id, text, _send_fn):
    """
    Returns True if command was handled, False otherwise.
    Add this call at top of _handle():
        if handle_options_commands(chat_id, text, _send):
            return
    """
    from options_paper_engine import get_engine
    from options_strategies import get_strategy_legs, place_strategy, STRATEGY_MAP
    from fyers_data import get_fyers

    engine = get_engine()
    parts  = text.strip().split()
    cmd    = parts[0].lower() if parts else ""

    # ── /optbuy NIFTY 24500 CE 26JUN 1 ──────────────────────────────────────
    if cmd == "/optbuy":
        if len(parts) < 6:
            _send_fn(chat_id,
                "Usage: /optbuy UNDERLYING STRIKE TYPE EXPIRY LOTS\n"
                "Ex: /optbuy NIFTY 24500 CE 26JUN 1")
            return True

        underlying = parts[1].upper()
        try:
            strike = int(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ Strike must be a number.")
            return True
        opt_type = parts[3].upper()
        expiry   = parts[4].upper()
        try:
            lots = int(parts[5])
        except ValueError:
            lots = 1

        fyers   = get_fyers()
        premium = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        if not premium:
            _send_fn(chat_id, f"❌ Could not fetch LTP for {underlying} {strike}{opt_type} {expiry}. Check symbol.")
            return True

        ok, msg = engine.place_order(underlying, strike, opt_type, expiry, "BUY", lots, premium)
        _send_fn(chat_id, msg)
        return True

    # ── /optsell NIFTY 24500 CE 26JUN 1 ─────────────────────────────────────
    elif cmd == "/optsell":
        if len(parts) < 6:
            _send_fn(chat_id,
                "Usage: /optsell UNDERLYING STRIKE TYPE EXPIRY LOTS\n"
                "Ex: /optsell NIFTY 24500 CE 26JUN 1")
            return True

        underlying = parts[1].upper()
        try:
            strike = int(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ Strike must be a number.")
            return True
        opt_type = parts[3].upper()
        expiry   = parts[4].upper()
        try:
            lots = int(parts[5])
        except ValueError:
            lots = 1

        fyers   = get_fyers()
        premium = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        if not premium:
            _send_fn(chat_id, f"❌ Could not fetch LTP for {underlying} {strike}{opt_type} {expiry}.")
            return True

        ok, msg = engine.place_order(underlying, strike, opt_type, expiry, "SELL", lots, premium)
        _send_fn(chat_id, msg)
        return True

    # ── /optclose NIFTY_24500CE_26JUN ────────────────────────────────────────
    elif cmd == "/optclose":
        if len(parts) < 2:
            _send_fn(chat_id, "Usage: /optclose KEY\nGet key from /optstatus")
            return True

        key = parts[1]
        pos = engine.get_position(key)
        if not pos:
            _send_fn(chat_id, f"❌ No position: {key}")
            return True

        fyers   = get_fyers()
        ltp     = _fetch_option_ltp(fyers, pos["underlying"], pos["strike"], pos["opt_type"], pos["expiry"])
        if not ltp:
            ltp = pos["ltp"]   # fallback to last known
            _send_fn(chat_id, f"⚠️ Using last known LTP ₹{ltp} (live fetch failed)")

        ok, msg = engine.close_position(key, ltp, reason="manual")
        _send_fn(chat_id, msg)
        return True

    # ── /optsl KEY PREMIUM ────────────────────────────────────────────────────
    elif cmd == "/optsl":
        if len(parts) < 3:
            _send_fn(chat_id, "Usage: /optsl KEY SL_PREMIUM\nEx: /optsl NIFTY_24500CE_26JUN 50")
            return True
        key = parts[1]
        try:
            sl = float(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ SL must be a number.")
            return True
        ok, msg = engine.set_sl(key, sl)
        _send_fn(chat_id, msg)
        return True

    # ── /opttp KEY PREMIUM ────────────────────────────────────────────────────
    elif cmd == "/opttp":
        if len(parts) < 3:
            _send_fn(chat_id, "Usage: /opttp KEY TP_PREMIUM\nEx: /opttp NIFTY_24500CE_26JUN 200")
            return True
        key = parts[1]
        try:
            tp = float(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ TP must be a number.")
            return True
        ok, msg = engine.set_tp(key, tp)
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
        _send_fn(chat_id,
            f"💰 <b>Options Paper Account</b>\n\n"
            f"Capital: ₹{s['capital']:,.0f}\n"
            f"Margin Used: ₹{s['used_margin']:,.0f}\n"
            f"Available: ₹{s['available']:,.0f}\n\n"
            f"Open PnL: ₹{s['open_pnl']:+,.0f}\n"
            f"Today PnL: ₹{day_pnl:+,.0f}\n"
            f"Total Realized: ₹{s['realized_pnl']:+,.0f}\n"
            f"Net PnL: ₹{s['net_pnl']:+,.0f}\n\n"
            f"Open Positions: {s['open_positions']}"
        )
        return True

    # ── /optlog ───────────────────────────────────────────────────────────────
    elif cmd == "/optlog":
        limit = 10
        if len(parts) > 1:
            try:
                limit = int(parts[1])
            except ValueError:
                pass

        log_entries = engine.get_trade_log(limit)
        if not log_entries:
            _send_fn(chat_id, "📭 No closed trades yet.")
            return True

        lines = [f"📜 <b>Last {len(log_entries)} Closed Option Trades</b>\n"]
        for t in reversed(log_entries):
            emoji = "🟢" if t["pnl"] >= 0 else "🔴"
            lines.append(
                f"{emoji} {t['underlying']} {t['strike']}{t['opt_type']} {t['expiry']}\n"
                f"   {t['action']} | Entry: ₹{t['entry_premium']} → ₹{t['exit_premium']}\n"
                f"   PnL: <b>₹{t['pnl']:+.0f}</b> | {t['result']} | {t['reason']}\n"
                f"   {t.get('date','')}"
            )
        _send_fn(chat_id, "\n\n".join(lines))
        return True

    # ── /strategy NIFTY straddle 26JUN 1 [sell] ──────────────────────────────
    elif cmd == "/strategy":
        # /strategy UNDERLYING NAME EXPIRY LOTS [sell/buy]
        if len(parts) < 5:
            avail = " | ".join(STRATEGY_MAP.keys())
            _send_fn(chat_id,
                f"Usage: /strategy UNDERLYING NAME EXPIRY LOTS [sell/buy]\n"
                f"Ex: /strategy NIFTY iron_condor 26JUN 1\n"
                f"Strategies: {avail}")
            return True

        underlying  = parts[1].upper()
        strat_name  = parts[2].lower()
        expiry      = parts[3].upper()
        try:
            lots = int(parts[4])
        except ValueError:
            lots = 1
        action = parts[5].upper() if len(parts) > 5 else "SELL"

        fyers = get_fyers()
        spot  = _fetch_spot(fyers, underlying)
        if not spot:
            _send_fn(chat_id, f"❌ Could not fetch spot for {underlying}")
            return True

        legs = get_strategy_legs(strat_name, underlying, spot, lots)
        if legs is None:
            avail = " | ".join(STRATEGY_MAP.keys())
            _send_fn(chat_id, f"❌ Unknown strategy: {strat_name}\nAvailable: {avail}")
            return True

        # Override action for straddle/strangle if specified
        if strat_name in ("straddle", "strangle"):
            from options_strategies import straddle, strangle
            fn   = straddle if strat_name == "straddle" else strangle
            legs = fn(underlying, spot, lots, action)

        # Fetch premiums for all legs
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

        _send_fn(chat_id, f"⏳ Placing {strat_name.upper()} ({len(legs)} legs)...")
        ok, msgs = place_strategy(engine, legs, expiry, premiums)

        summary = f"{'✅' if ok else '❌'} <b>{strat_name.upper()}</b> {'placed' if ok else 'failed'}\n\n"
        summary += "\n\n".join(msgs)
        _send_fn(chat_id, summary)
        return True

    # ── /optreset ─────────────────────────────────────────────────────────────
    elif cmd == "/optreset":
        if len(parts) > 1 and parts[1].upper() == "CONFIRM":
            msg = engine.reset()
            _send_fn(chat_id, msg)
        else:
            _send_fn(chat_id,
                "⚠️ This will wipe ALL option positions and reset capital to ₹5,00,000.\n"
                "Type /optreset CONFIRM to proceed.")
        return True

    # ── /optsettle NIFTY 26JUN 24500 ─────────────────────────────────────────
    elif cmd == "/optsettle":
        if len(parts) < 4:
            _send_fn(chat_id, "Usage: /optsettle UNDERLYING EXPIRY SPOT_PRICE\nEx: /optsettle NIFTY 26JUN 24500")
            return True
        underlying = parts[1].upper()
        expiry     = parts[2].upper()
        try:
            spot = float(parts[3])
        except ValueError:
            _send_fn(chat_id, "❌ Spot price must be a number.")
            return True

        msgs = engine.settle_expiry(expiry, spot)
        if not msgs:
            _send_fn(chat_id, f"No positions found for expiry {expiry}.")
        else:
            _send_fn(chat_id, "\n\n".join(msgs))
        return True

    # ── /optprice NIFTY 24500 CE 26JUN ───────────────────────────────────────
    elif cmd == "/optprice":
        # Also handle bare key like NIFTY_24500CE_26JUN (no slash, no command)
        if len(parts) < 5:
            _send_fn(chat_id,
                "Usage: /optprice UNDERLYING STRIKE TYPE EXPIRY\n"
                "Ex: /optprice NIFTY 24500 CE 26JUN")
            return True

        underlying = parts[1].upper()
        try:
            strike = int(parts[2])
        except ValueError:
            _send_fn(chat_id, "❌ Strike must be a number.")
            return True
        opt_type = parts[3].upper()
        expiry   = parts[4].upper()

        fyers = get_fyers()
        ltp   = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        spot  = _fetch_spot(fyers, underlying)

        if not ltp:
            _send_fn(chat_id,
                f"❌ Could not fetch price for {underlying} {strike}{opt_type} {expiry}.\n"
                f"Check the symbol/expiry format.")
            return True

        spot_str = f"₹{spot:,.2f}" if spot else "N/A"
        _send_fn(chat_id,
            f"📊 <b>{underlying} {strike} {opt_type} {expiry}</b>\n\n"
            f"Premium LTP: <b>₹{ltp:.2f}</b>\n"
            f"{underlying} Spot: {spot_str}"
        )
        return True

    # ── /opthelp ──────────────────────────────────────────────────────────────
    elif cmd == "/opthelp":
        _send_fn(chat_id,
            "🎯 <b>Options Paper Trading Commands</b>\n\n"
            "<b>Price Check</b>\n"
            "/optprice NIFTY 24500 CE 26JUN  — live premium LTP\n\n"
            "<b>Single Leg</b>\n"
            "/optbuy NIFTY 24500 CE 26JUN 1\n"
            "/optsell NIFTY 24500 CE 26JUN 1\n\n"
            "<b>Manage</b>\n"
            "/optclose KEY        — close position\n"
            "/optsl KEY PREMIUM   — set stop loss\n"
            "/opttp KEY PREMIUM   — set target\n\n"
            "<b>Multi-Leg Strategies</b>\n"
            "/strategy NIFTY straddle 26JUN 1\n"
            "/strategy NIFTY strangle 26JUN 1 sell\n"
            "/strategy NIFTY bull_call 26JUN 1\n"
            "/strategy NIFTY bear_put 26JUN 1\n"
            "/strategy NIFTY bull_put 26JUN 1\n"
            "/strategy NIFTY bear_call 26JUN 1\n"
            "/strategy NIFTY iron_condor 26JUN 1\n"
            "/strategy NIFTY iron_butterfly 26JUN 1\n\n"
            "<b>Account</b>\n"
            "/optstatus  — all open positions\n"
            "/optpnl     — account summary\n"
            "/optlog     — trade history\n"
            "/optreset   — reset account\n\n"
            "<b>Expiry</b>\n"
            "/optsettle NIFTY 26JUN 24500  — manual settle"
        )
        return True

    return False   # command not handled here