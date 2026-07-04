"""
options_monitor.py
==================
Background thread — polls Fyers LTP every N seconds for all open
option positions. Triggers SL/TP auto-close via OptionsPaperEngine.

Improvements over v1:
  - Consecutive fetch failure counter + Telegram alert
  - Market hours check (skip polling outside 09:15-15:35 IST)
  - Expiry settlement deduplicated (won't double-settle same expiry)
  - EOD exit only fires once (flag-based, not time-window-based)
  - Symbol build failure logged explicitly
  - Fyers re-auth on None client with backoff

Usage:
    from options_monitor import start_monitor
    start_monitor(fyers_getter, engine, notify_fn)
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

POLL_INTERVAL      = 60    # seconds between polls
MAX_FETCH_FAILURES = 3     # alert after this many consecutive failures
MARKET_OPEN_H      = 9
MARKET_OPEN_M      = 15
MARKET_CLOSE_H     = 15
MARKET_CLOSE_M     = 35


def _is_market_open() -> bool:
    """Check if current IST time is within market hours."""
    now = datetime.now()
    t   = now.hour * 60 + now.minute
    open_t  = MARKET_OPEN_H  * 60 + MARKET_OPEN_M
    close_t = MARKET_CLOSE_H * 60 + MARKET_CLOSE_M
    return open_t <= t <= close_t


def _build_fyers_symbol(pos: dict) -> Optional[str]:
    """
    Build Fyers symbol string from position dict.
    Returns None and logs error if format unrecognized.
    """
    underlying = pos["underlying"]
    expiry     = pos["expiry"].upper()
    strike     = pos["strike"]
    opt_type   = pos["opt_type"]
    exchange   = "BSE" if underlying == "SENSEX" else "NSE"

    MONTHS = {
        "JAN": "1",  "FEB": "2",  "MAR": "3",  "APR": "4",
        "MAY": "5",  "JUN": "6",  "JUL": "7",  "AUG": "8",
        "SEP": "9",  "OCT": "10", "NOV": "11", "DEC": "12",
    }

    try:
        day = expiry[:2]
        mon = expiry[2:]
        mm  = MONTHS.get(mon)
        if mm is None:
            log.warning(f"[MON] Unknown month in expiry: {expiry} for {pos['key']}")
            return None
        yy  = datetime.now().strftime("%y")
        day_int = int(day)
    except Exception as e:
        log.warning(f"[MON] Symbol build failed for {pos.get('key', '?')}: {e}")
        return None

    if day_int >= 25:
        return f"{exchange}:{underlying}{yy}{mon}{strike}{opt_type}"
    else:
        return f"{exchange}:{underlying}{yy}{mm}{day}{strike}{opt_type}"


def _fetch_ltps(fyers, symbols: list) -> dict:
    """
    Fetch LTP for multiple symbols in one call.
    Returns {symbol: ltp}. Batches of 10 to respect Fyers limits.
    """
    if not symbols:
        return {}
    result = {}
    batch_size = 10
    try:
        from fyers_data import get_quotes
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            log.info(f"[MON] Fetching symbols: {batch}")
            quotes = get_quotes(fyers, batch)
            if quotes:
                for q in quotes:
                    sym = q.get("n") or q.get("symbol", "")
                    ltp = q.get("v", {}).get("lp") or q.get("lp")
                    if sym and ltp:
                        result[sym] = float(ltp)
            if i + batch_size < len(symbols):
                time.sleep(2)
        return result
    except Exception as e:
        log.warning(f"[MON] LTP fetch failed: {e}")
        return {}


def _eod_exit(engine, notify_fn, ltps: dict, sym_key_map: dict, eod_done_set: set):
    """
    Auto-close ALL open positions at 3:30 PM IST (EOD).
    Uses eod_done_set to ensure it only fires once per day.
    """
    now = datetime.now()
    if not (now.hour == 15 and 30 <= now.minute <= 35):
        return

    day_key = now.strftime("%Y-%m-%d")
    if day_key in eod_done_set:
        return

    open_positions = engine.get_open_positions()
    if not open_positions:
        eod_done_set.add(day_key)
        return

    log.info("[MON] EOD exit triggered — closing all open positions.")
    eod_done_set.add(day_key)

    key_sym_map = {v: k for k, v in sym_key_map.items()}

    for pos in open_positions:
        key       = pos["key"]
        fyers_sym = key_sym_map.get(key)
        ltp       = ltps.get(fyers_sym) if fyers_sym else None
        if ltp is None:
            ltp = pos.get("ltp", pos["entry_premium"])

        ok, msg = engine.close_position(key, ltp, reason="eod_exit")
        if ok:
            log.info(f"[MON] EOD closed: {key} @ {ltp}")
            try:
                notify_fn(f"🔔 <b>EOD EXIT</b>\n{msg}")
            except Exception as e:
                log.warning(f"[MON] EOD notify failed: {e}")


def _check_expiry_settlement(engine, notify_fn, settled_set: set):
    """
    Auto-settle positions on expiry day at 3:30pm.
    Uses settled_set to prevent double-settlement of same expiry.
    """
    now = datetime.now()
    if now.hour != 15 or now.minute < 30:
        return

    open_positions = engine.get_open_positions()
    today_str      = now.strftime("%d%b").upper()

    expiring = [p for p in open_positions if p["expiry"].upper() == today_str]
    if not expiring:
        return

    settle_key = f"{today_str}_{now.strftime('%Y-%m-%d')}"
    if settle_key in settled_set:
        return

    log.info(f"[MON] Expiry settlement triggered for {today_str}")
    settled_set.add(settle_key)

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


def _monitor_loop(fyers_getter, engine, notify_fn, stop_event: threading.Event):
    """
    Main monitor loop.
    fyers_getter: callable -> returns fyers client
    engine: OptionsPaperEngine instance
    notify_fn: callable(msg) -> Telegram send
    """
    log.info("[MON] Options monitor started.")

    fetch_failures = {}    # {batch_key: count}
    eod_done_set   = set() # tracks EOD per day
    settled_set    = set() # tracks settled expiries
    auth_backoff   = 0     # seconds to wait before retrying fyers

    while not stop_event.is_set():
        try:
            # Skip polling outside market hours
            if not _is_market_open():
                time.sleep(POLL_INTERVAL)
                continue

            open_positions = engine.get_open_positions()
            if not open_positions:
                time.sleep(POLL_INTERVAL)
                continue

            # Build symbol → key map (skip positions with bad symbol format)
            sym_key_map = {}
            for pos in open_positions:
                sym = _build_fyers_symbol(pos)
                if sym:
                    sym_key_map[sym] = pos["key"]
                else:
                    log.warning(f"[MON] Could not build symbol for {pos['key']} — skipping")

            if not sym_key_map:
                time.sleep(POLL_INTERVAL)
                continue

            # Fyers auth check with backoff
            if auth_backoff > 0:
                time.sleep(auth_backoff)
                auth_backoff = 0

            fyers = fyers_getter()
            if fyers is None:
                log.warning("[MON] Fyers not authenticated. Backing off 3 mins.")
                auth_backoff = 180
                continue

            ltps = _fetch_ltps(fyers, list(sym_key_map.keys()))

            # Track fetch failures
            if not ltps:
                batch_key = "all"
                fetch_failures[batch_key] = fetch_failures.get(batch_key, 0) + 1
                if fetch_failures[batch_key] == MAX_FETCH_FAILURES:
                    try:
                        notify_fn(f"⚠️ <b>Monitor Alert</b>: LTP fetch failed {MAX_FETCH_FAILURES} consecutive times. Check Fyers connection.")
                    except Exception:
                        pass
                    log.warning(f"[MON] {MAX_FETCH_FAILURES} consecutive LTP fetch failures.")
            else:
                fetch_failures["all"] = 0

            # EOD exit
            _eod_exit(engine, notify_fn, ltps, sym_key_map, eod_done_set)

            # SL/TP checks
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

                    # Check if trading halted after close
                    halted, halt_reason = engine.is_halted()
                    if halted:
                        try:
                            notify_fn(f"🛑 <b>Trading Halted</b>\n{halt_reason}\nUse /optresume to re-enable.")
                        except Exception:
                            pass

            # Expiry settlement
            _check_expiry_settlement(engine, notify_fn, settled_set)

        except Exception as e:
            log.warning(f"[MON] loop error: {e}")

        time.sleep(POLL_INTERVAL)

    log.info("[MON] Options monitor stopped.")


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


# Fix missing import
