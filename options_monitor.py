"""
options_monitor.py
==================
Background thread — polls Fyers LTP every N seconds for all open
option positions. Triggers SL/TP auto-close via OptionsPaperEngine.

Usage:
    from options_monitor import start_monitor
    start_monitor(fyers, engine, notify_fn)
"""

import logging
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

POLL_INTERVAL = 5   # seconds between LTP polls


def _build_fyers_symbol(pos: dict) -> str:
    underlying = pos["underlying"]
    expiry     = pos["expiry"].upper()
    strike     = pos["strike"]
    opt_type   = pos["opt_type"]
    exchange   = "BSE" if underlying == "SENSEX" else "NSE"

    MONTHS = {"JAN":"1","FEB":"2","MAR":"3","APR":"4",
          "MAY":"5","JUN":"6","JUL":"7","AUG":"8",
          "SEP":"9","OCT":"10","NOV":"11","DEC":"12"}

    day = expiry[:2]
    mon = expiry[2:]
    mm  = MONTHS.get(mon, "06")
    yy  = datetime.now().strftime("%y")

    if int(day) >= 25:
        # Monthly expiry — Fyers uses DDMMM format e.g. NSE:NIFTY26JUN24500CE
        symbol = f"{exchange}:{underlying}{expiry}{strike}{opt_type}"
    else:
        # Weekly expiry — Fyers uses YYMMDD format e.g. NSE:NIFTY2660923150PE
        symbol = f"{exchange}:{underlying}{yy}{mm}{day}{strike}{opt_type}"

    return symbol


def _fetch_ltps(fyers, symbols: list[str]) -> dict:
    """
    Fetch LTP for multiple symbols in one call.
    Returns {symbol: ltp}
    """
    if not symbols:
        return {}
    try:
        from fyers_data import get_quotes
        log.info(f"[MON] Fetching symbols: {symbols}")
        quotes = get_quotes(fyers, symbols)
        result = {}
        if quotes:
            for q in quotes:
                sym = q.get("n") or q.get("symbol", "")
                ltp = q.get("v", {}).get("lp") or q.get("lp")
                if sym and ltp:
                    result[sym] = float(ltp)
        return result
    except Exception as e:
        log.warning(f"[MON] LTP fetch failed: {e}")
        return {}


def _monitor_loop(fyers_getter, engine, notify_fn, stop_event: threading.Event):
    """
    Main monitor loop.
    fyers_getter: callable -> returns fyers client (handles re-auth)
    engine: OptionsPaperEngine instance
    notify_fn: callable(msg: str) -> sends Telegram message
    """
    log.info("[MON] Options monitor started.")

    while not stop_event.is_set():
        try:
            open_positions = engine.get_open_positions()
            if not open_positions:
                time.sleep(POLL_INTERVAL)
                continue

            # Build symbol -> key map
            sym_key_map = {}
            for pos in open_positions:
                sym = _build_fyers_symbol(pos)
                sym_key_map[sym] = pos["key"]

            fyers = fyers_getter()
            if fyers is None:
                log.warning("[MON] Fyers not authenticated. Skipping poll.")
                time.sleep(POLL_INTERVAL * 3)
                continue

            ltps = _fetch_ltps(fyers, list(sym_key_map.keys()))

            for sym, pos_key in sym_key_map.items():
                ltp = ltps.get(sym)
                if ltp is None:
                    continue

                trigger = engine.update_ltp(pos_key, ltp)

                if trigger:
                    parts      = trigger.split(":")
                    trig_type  = parts[0]
                    trig_key   = parts[1]
                    trig_price = float(parts[2]) if len(parts) > 2 else ltp

                    reason = "sl_hit" if trig_type == "SL_HIT" else "tp_hit"
                    ok, msg = engine.close_position(trig_key, trig_price, reason=reason)

                    if ok:
                        emoji = "🛑" if reason == "sl_hit" else "🎯"
                        alert = (
                            f"{emoji} <b>{'STOP LOSS' if reason == 'sl_hit' else 'TARGET'} HIT</b>\n"
                            f"{msg}"
                        )
                        log.info(f"[MON] {trig_type} for {trig_key} @ {trig_price}")
                        try:
                            notify_fn(alert)
                        except Exception as e:
                            log.warning(f"[MON] notify failed: {e}")

            _check_expiry_settlement(engine, notify_fn)

        except Exception as e:
            log.warning(f"[MON] loop error: {e}")

        time.sleep(POLL_INTERVAL)

    log.info("[MON] Options monitor stopped.")


def _check_expiry_settlement(engine, notify_fn):
    """Auto-settle positions on expiry day at 3:30pm."""
    now = datetime.now()
    if now.hour != 15 or now.minute < 30:
        return

    open_positions = engine.get_open_positions()
    today_str = now.strftime("%d%b").upper()

    expiring = [p for p in open_positions if p["expiry"].upper() == today_str]
    if not expiring:
        return

    log.info(f"[MON] Expiry settlement triggered for {today_str}")

    try:
        from fyers_data import get_fyers, get_quotes
        fyers = get_fyers()
        SPOT_SYMBOLS = {
            "NIFTY":     "NSE:NIFTY50-INDEX",
            "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
            "SENSEX":    "BSE:SENSEX-INDEX",
        }
        underlyings = set(p["underlying"] for p in expiring)
        for underlying in underlyings:
            spot_sym = SPOT_SYMBOLS.get(underlying)
            if not spot_sym:
                continue
            quotes = get_quotes(fyers, [spot_sym])
            if quotes:
                spot = float(quotes[0]["v"]["lp"])
                msgs = engine.settle_expiry(today_str, spot)
                for msg in msgs:
                    log.info(f"[MON] Settled: {msg}")
                    try:
                        notify_fn(f"⏰ <b>EXPIRY SETTLEMENT</b>\n{msg}")
                    except Exception:
                        pass
    except Exception as e:
        log.warning(f"[MON] expiry settlement error: {e}")


def start_monitor(fyers_getter, engine, notify_fn) -> threading.Event:
    """
    Start background monitor thread.
    Returns stop_event — call stop_event.set() to stop.
    """
    stop_event = threading.Event()
    t = threading.Thread(
        target=_monitor_loop,
        args=(fyers_getter, engine, notify_fn, stop_event),
        name="OptionsMonitor",
        daemon=True,
    )
    t.start()
    log.info("[MON] Monitor thread launched.")
    return stop_event