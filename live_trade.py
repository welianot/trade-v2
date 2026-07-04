"""
live_trade.py
=============
Live paper-trading runner for Delta India Demo.
Strategy: 4H Liquidity Grab + 15M SMC Entry (BOS + FVG)

Improvements integrated:
- ATR-based stop-loss (adaptive to volatility)
- Partial take-profit (close 50% at 1:2 RR, let rest trail)
- ADX-based dynamic RR (strong trends get bigger targets)
- Volume confirmation on BOS candle
- Retry logic for API calls
- 30-second polling with live price checks
- Pending limit order management with timeout
"""

import ccxt
import csv
import json
import os
import sys
try:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding='utf-8')
except Exception:
    pass
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import pandas as pd
from telegram_alerts import send as tg
from bot_server import start as start_bot
from state_manager import (
    save_positions, save_daily,
    load_positions, load_daily,
    reconcile_positions,
    save_seen_grabs, load_seen_grabs,
)

# ─── Broadcast dedup (persisted across restarts) ─────────────────────────────

BROADCAST_SEEN_FILE = "seen_broadcasts.json"

def _load_broadcast_seen() -> set:
    try:
        with open(BROADCAST_SEEN_FILE, "r") as f:
            data = json.load(f)
            # Prune entries older than 24h
            now_ts = time.time()
            fresh = {k: v for k, v in data.items() if now_ts - v < 86400}
            return set(fresh.keys()), fresh
    except Exception:
        return set(), {}

def _save_broadcast_seen(seen_dict: dict):
    try:
        with open(BROADCAST_SEEN_FILE, "w") as f:
            json.dump(seen_dict, f)
    except Exception:
        pass
from back_test import (
    add_emas, detect_liquidity_grabs, detect_bos, detect_fvg,
    SYMBOL_CONFIG, MIN_RR, RISK_PER_TRADE, ACCOUNT_SIZE,
    SWING_LOOKBACK, ENTRY_WINDOW, MAX_TRADES_PER_DAY, DAILY_LOSS_LIMIT,
)

# ─── CONFIG ─────────────────────────────────────────────────────────────────

DEMO_HOST    = "https://cdn-ind.testnet.deltaex.org"
LOG_FILE     = "trades_log.csv"
POLL_SECONDS = 60
CANDLES_4H   = 300
CANDLES_15M  = 800

# Adjustable parameters
ATR_MULTIPLIER  = 1.5          # SL distance = ATR * multiplier
PARTIAL_RR      = 2.0          # RR for partial close (50%)
ADX_STRONG      = 25           # ADX above this => use 1:4 RR
ADX_MODERATE    = 20           # ADX above this => use 1:3 RR, else 1:2 RR
VOLUME_SPIKE    = 1.5          # BOS volume must be > avg * multiplier (raised from 1.2 — filters fake breakouts)
ORDER_TIMEOUT   = 900          # cancel pending limit order after 15 min
POLL_INTERVAL   = 30           # seconds between scans
PRICE_PROXIMITY = 0.02         # max % gap between live price and FVG entry (2% = tight, avoids chasing)

# Map strategy symbol keys → ccxt unified symbol + contract size
SYMBOL_MAP = {
    "BTCUSDT": {"ccxt": "BTC/USD:USD", "contract_size": 0.001},
    "ETHUSDT": {"ccxt": "ETH/USD:USD", "contract_size": 0.01},
}

CSV_FIELDS = [
    "date", "symbol", "side", "lots", "contract_size", "entry_price", "exit_price",
    "sl_price", "tp_price", "pnl_usd", "result",
    "hold_time_min", "opened_at", "closed_at",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("live_trade.log"),
    ],
)
log = logging.getLogger(__name__)

# ─── RETRY WRAPPER ──────────────────────────────────────────────────────────

def retry(func, *args, retries=3, delay=2, **kwargs):
    """Retry a function with exponential backoff."""
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(delay * (i + 1))
    return None

# ─── EXCHANGE ────────────────────────────────────────────────────────────────

def _load_env_file():
    env = {}
    try:
        with open(".env", "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def make_exchange():
    env = _load_env_file()
    key = env.get("API_KEY") or os.getenv("API_KEY", "")
    sec = (env.get("API_SECRET") or env.get("API_SCECRET") or os.getenv("API_SECRET") or os.getenv("API_SCECRET") or "")
    ex = ccxt.delta({"apiKey": key or "", "secret": sec or "", "enableRateLimit": True,"options":{"adjustForTimeDifference": True}})
    ex.urls = ex.urls or {}
    ex.urls["api"] = {"public": DEMO_HOST, "private": DEMO_HOST}
    ex.verbose = False
    return ex

# ─── CSV ─────────────────────────────────────────────────────────────────────

def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

def append_csv(row):
    with open(LOG_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def fetch_candles(ex, ccxt_sym, timeframe, limit):
    try:
        import requests
        delta_sym  = ccxt_sym.split("/")[0] + "USD"
        tf_seconds = ex.parse_timeframe(timeframe)
        now        = int(time.time())
        start      = now - tf_seconds * limit

        for params in [
            {"symbol": delta_sym, "resolution": timeframe, "start": start, "end": now },
            {"symbol": delta_sym, "resolution": timeframe, "start": start,"end": now-60},
        ]:
            resp = requests.get(f"{DEMO_HOST}/v2/history/candles", params=params)
            data = resp.json()
            if data.get("success"):
                break
        
        if not data.get("success"):
            log.warning(f"fetch_candles {ccxt_sym} {timeframe}: {data}")
            return None

        candles = data.get("result", [])
        if not candles:
            return None

        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s")
        df = df[["timestamp","open","high","low","close","volume"]]
        df = df.set_index("timestamp").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        return df
    except Exception as e:
        log.warning(f"fetch_candles {ccxt_sym} {timeframe}: {e}")
        return None
    
def get_balance(ex):
    try:
        b = ex.fetch_balance()
        return float(b.get("total", {}).get("USD", ACCOUNT_SIZE))
    except Exception:
        return ACCOUNT_SIZE


def has_open_position(ex, ccxt_sym):
    try:
        pos = ex.fetch_positions([ccxt_sym])
        return any(p.get("contracts") and float(p["contracts"]) != 0 for p in pos)
    except Exception:
        return False


def calc_lots(equity, entry, sl, contract_size):
    risk_amount = equity * RISK_PER_TRADE
    sl_dist     = abs(entry - sl)
    if sl_dist <= 0:
        return 0
    coin_qty = risk_amount / sl_dist
    lots     = max(1, round(coin_qty / contract_size))
    return lots


def place_bracket_limit(ex, ccxt_sym, side, lots, entry, sl, tp):
    order = ex.create_order(
        ccxt_sym, "limit", side, lots, entry,
        params={
            # "postOnly": True,   # uncomment if Delta supports
            "bracket_stop_loss_price": str(round(sl, 2)),
            "bracket_stop_loss_limit_price": str(round(sl, 2)),
            "bracket_take_profit_price": str(round(tp, 2)),
            "bracket_take_profit_limit_price": str(round(tp, 2)),
        },
    )
    return order

# ─── INDICATOR HELPERS ──────────────────────────────────────────────────────

def calculate_atr(df, period=14):
    """Compute Average True Range for the given DataFrame."""
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr.iloc[-1]

def calculate_adx(df, period=14):
    """Compute ADX (Average Directional Index) for the given DataFrame."""
    high = df['high']
    low = df['low']
    close = df['close']
    plus_dm = high.diff()
    minus_dm = low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (abs(minus_dm).rolling(period).mean() / atr)
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.rolling(period).mean()
    return adx.iloc[-1]

# ─── HEALTH PING ─────────────────────────────────────────────────────────────

def _health_ping_loop(tracker, daily_trades, daily_loss, ex, kill_switch):
    PING_INTERVAL = 3600
    while True:
        time.sleep(PING_INTERVAL)
        try:
            equity = get_balance(ex)
            day_key = datetime.now(timezone.utc).date()
            trades_today = daily_trades.get(day_key, 0)
            loss_today   = daily_loss.get(day_key, 0.0)
            
            with tracker._lock:
                open_count = len(tracker.open)
                open_snapshot = dict(tracker.open)
            
            paused = "⏸ PAUSED" if kill_switch.is_set() else "▶ RUNNING"

            open_lines = ""
            if open_snapshot:
                lines = []
                for sym_key, m in open_snapshot.items():
                    try:
                        ccxt_sym = m["ccxt_sym"]
                        px = float(ex.fetch_ticker(ccxt_sym)["last"])
                        sign = 1 if m["side"] == "buy" else -1
                        upnl = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
                        lines.append(f"  • {sym_key} {m['side'].upper()} | uPnL: {upnl:+.4f} USD")
                    except Exception:
                        lines.append(f"  • {sym_key} {m['side'].upper()} | uPnL: N/A")
                open_lines = "\n" + "\n".join(lines)

            tg(
                f"💓 <b>Health Ping</b>\n"
                f"Status: {paused}\n"
                f"Equity: ${equity:.2f}\n"
                f"Open trades: {open_count}{open_lines}\n"
                f"Today: {trades_today} trades | Loss: {loss_today:.4f} USD"
            )
        except Exception as e:
            log.warning(f"[HEALTH] ping failed: {e}")

# ─── SIGNAL DETECTION ────────────────────────────────────────────────────────

def prune_seen_grabs(seen_grabs, days=7):
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts = now_ts - (days * 86400)
    stale_keys = [k for k, ts in seen_grabs.items() if isinstance(ts, (int, float)) and ts < cutoff_ts]
    for k in stale_keys:
        del seen_grabs[k]
    if stale_keys:
        log.info(f"  Pruned {len(stale_keys)} old grab keys (>7 days)")
    return seen_grabs


# ─── CONTINUATION + RETRACEMENT SIGNALS ─────────────────────────────────────

def find_ema_retracement_signal(sym_key, df_4h, df_15m, ex):
    """
    Continuation/retracement entry:
    - 4H trend is clear (price > EMA200 for longs, < EMA200 for shorts)
    - ADX > 20 (trending, not ranging)
    - 15M price pulls back to EMA50 or EMA21 and shows a rejection candle
    - Entry at close of rejection candle
    - SL below the pullback low (long) or above the pullback high (short)

    This catches moves AFTER the trend is established — no liquidity grab needed.
    """
    cfg = SYMBOL_CONFIG.get(sym_key, SYMBOL_CONFIG["ETHUSDT"])

    df_4h = add_emas(df_4h.copy())
    df_15m = df_15m.copy()

    # Add 15M EMAs
    df_15m["ema21"] = df_15m["close"].ewm(span=21, adjust=False).mean()
    df_15m["ema50"] = df_15m["close"].ewm(span=50, adjust=False).mean()

    try:
        ema200_4h  = df_4h["ema200"].iloc[-1]
        ema50_4h   = df_4h["ema50"].iloc[-1]
        slope_4h   = df_4h["ema50_slope"].iloc[-1]
        last_close = df_4h["close"].iloc[-1]
    except (IndexError, KeyError):
        return None

    # ADX filter — only trade in trending markets
    try:
        adx = calculate_adx(df_4h)
    except Exception:
        adx = 0
    if adx < ADX_MODERATE:
        log.info(f"  [RETRACE] {sym_key} ADX={adx:.1f} < {ADX_MODERATE} — ranging, skip")
        return None

    # Determine trend direction
    is_bullish_trend = last_close > ema200_4h and slope_4h > 0
    is_bearish_trend = last_close < ema200_4h and slope_4h < 0

    if not is_bullish_trend and not is_bearish_trend:
        log.info(f"  [RETRACE] {sym_key} no clear trend — skip")
        return None

    direction = "long" if is_bullish_trend else "short"

    # Look at last 10 15M candles for retracement to EMA
    lookback = 10
    recent = df_15m.iloc[-lookback:]

    for i in range(len(recent) - 1, 0, -1):
        candle = recent.iloc[i]
        prev   = recent.iloc[i - 1]
        ema21  = recent["ema21"].iloc[i]
        ema50  = recent["ema50"].iloc[i]

        if direction == "long":
            # Price touched EMA21 or EMA50 (low went below it) but closed above
            touched_ema = candle["low"] <= ema21 * 1.001 or candle["low"] <= ema50 * 1.001
            closed_above = candle["close"] > ema21
            # Rejection candle: close > open (bullish candle after touching EMA)
            is_rejection = candle["close"] > candle["open"]

            if touched_ema and closed_above and is_rejection:
                entry = candle["close"]
                # SL below the wick low of the rejection candle
                atr = calculate_atr(df_15m)
                sl  = round(min(candle["low"], prev["low"]) - atr * 0.3, 2)
                risk = entry - sl
                if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                    continue
                rr = 4 if adx > ADX_STRONG else 3
                tp = round(entry + risk * rr, 2)

                # Live price check
                try:
                    ticker = ex.fetch_ticker(SYMBOL_MAP[sym_key]["ccxt"])
                    live_px = ticker["last"]
                    if abs(live_px - entry) / entry > PRICE_PROXIMITY * 1.5:
                        continue
                except Exception:
                    pass

                log.info(f"  [RETRACE LONG] {sym_key} EMA retracement at {entry:.2f} SL={sl:.2f} TP={tp:.2f} ADX={adx:.1f}")
                return "long", entry, sl, tp

        else:  # short
            touched_ema = candle["high"] >= ema21 * 0.999 or candle["high"] >= ema50 * 0.999
            closed_below = candle["close"] < ema21
            is_rejection = candle["close"] < candle["open"]

            if touched_ema and closed_below and is_rejection:
                entry = candle["close"]
                atr = calculate_atr(df_15m)
                sl  = round(max(candle["high"], prev["high"]) + atr * 0.3, 2)
                risk = sl - entry
                if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                    continue
                rr = 4 if adx > ADX_STRONG else 3
                tp = round(entry - risk * rr, 2)

                try:
                    ticker = ex.fetch_ticker(SYMBOL_MAP[sym_key]["ccxt"])
                    live_px = ticker["last"]
                    if abs(live_px - entry) / entry > PRICE_PROXIMITY * 1.5:
                        continue
                except Exception:
                    pass

                log.info(f"  [RETRACE SHORT] {sym_key} EMA retracement at {entry:.2f} SL={sl:.2f} TP={tp:.2f} ADX={adx:.1f}")
                return "short", entry, sl, tp

    return None


def find_continuation_signal(sym_key, df_4h, df_15m, ex):
    """
    Continuation entry after 15M BOS — no liquidity grab required.
    - 4H trend confirmed (EMA200 + slope)
    - 15M breaks a recent swing high/low (BOS)
    - 15M FVG forms after BOS
    - Entry at FVG midpoint on retest

    This catches the first pullback after a fresh breakout.
    """
    cfg = SYMBOL_CONFIG.get(sym_key, SYMBOL_CONFIG["ETHUSDT"])
    df_4h  = add_emas(df_4h.copy())
    df_15m = df_15m.copy()

    try:
        ema200_4h  = df_4h["ema200"].iloc[-1]
        slope_4h   = df_4h["ema50_slope"].iloc[-1]
        last_close = df_4h["close"].iloc[-1]
    except (IndexError, KeyError):
        return None

    try:
        adx = calculate_adx(df_4h)
    except Exception:
        adx = 0

    is_bullish_trend = last_close > ema200_4h and slope_4h > 0
    is_bearish_trend = last_close < ema200_4h and slope_4h < 0

    if not is_bullish_trend and not is_bearish_trend:
        return None

    direction = "long" if is_bullish_trend else "short"

    # Look for BOS in last 20 15M candles
    scan_start = max(0, len(df_15m) - 20)
    bos_idx = detect_bos(df_15m, scan_start, direction, window=16)
    if bos_idx is None:
        return None

    # Volume confirmation at BOS
    try:
        vol_series = df_15m["volume"]
        avg_vol    = vol_series.iloc[max(0, bos_idx - 20):bos_idx].mean()
        if vol_series.iloc[bos_idx] < avg_vol * VOLUME_SPIKE:
            log.info(f"  [CONT] {sym_key} BOS volume weak — skip")
            return None
    except Exception:
        pass

    # FVG after BOS
    entry = sl = tp = None
    for j in range(bos_idx + 1, min(bos_idx + 8, len(df_15m) - 1)):
        fvg = detect_fvg(df_15m, j)
        if fvg is None:
            continue
        fvg_type, fvg_top, fvg_bot = fvg
        fvg_mid = (fvg_top + fvg_bot) / 2

        if direction == "long" and fvg_type == "bullish":
            entry = fvg_mid
            atr   = calculate_atr(df_15m)
            sl    = round(entry - atr * ATR_MULTIPLIER, 2)
            risk  = entry - sl
            if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                continue
            rr = 4 if adx > ADX_STRONG else 3 if adx > ADX_MODERATE else 2
            tp = round(entry + risk * rr, 2)
            break

        elif direction == "short" and fvg_type == "bearish":
            entry = fvg_mid
            atr   = calculate_atr(df_15m)
            sl    = round(entry + atr * ATR_MULTIPLIER, 2)
            risk  = sl - entry
            if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                continue
            rr = 4 if adx > ADX_STRONG else 3 if adx > ADX_MODERATE else 2
            tp = round(entry - risk * rr, 2)
            break

    if entry is None:
        return None

    # Live price proximity
    try:
        ticker  = ex.fetch_ticker(SYMBOL_MAP[sym_key]["ccxt"])
        live_px = ticker["last"]
        if abs(live_px - entry) / entry > PRICE_PROXIMITY:
            log.info(f"  [CONT] {sym_key} price {live_px:.2f} too far from FVG {entry:.2f}")
            return None
    except Exception:
        pass

    log.info(f"  [CONT {direction.upper()}] {sym_key} entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} ADX={adx:.1f}")
    return direction, entry, sl, tp

def find_fresh_signal(sym_key, df_4h, df_15m, seen_grabs, equity, ex):
    """
    Returns (direction, entry, sl, tp) if fresh signal found, else None.
    Uses ATR for SL, ADX for dynamic RR, volume spike confirmation.
    """
    prune_seen_grabs(seen_grabs, days=7)
    
    cfg = SYMBOL_CONFIG.get(sym_key, SYMBOL_CONFIG["ETHUSDT"])

    df_4h = add_emas(df_4h.copy())
    grabs = detect_liquidity_grabs(df_4h, min_wick_pct=cfg["min_wick_pct"])
    log.info(f"  [DEBUG] {sym_key} grabs_found={len(grabs)}")
    if grabs.empty:
        return None

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    now_ts = datetime.now(timezone.utc).timestamp()

    for _, grab in grabs.iloc[::-1].iterrows():
        grab_key  = str(grab["grab_time"])
        grab_type = grab["grab_type"]
        direction = "long" if grab_type == "bullish" else "short"
        grab_time = grab["grab_time"]

        age_hours = (now_utc - grab_time).total_seconds() / 3600
        log.info(f"  [DEBUG] {sym_key} grab={grab_key} age={age_hours:.1f}h seen={grab_key in seen_grabs} type={grab_type}")
        if age_hours > 48:
            seen_grabs[grab_key] = now_ts
            continue

        if grab_key in seen_grabs:
            continue

        # Trend filter
        try:
            slope      = df_4h.loc[df_4h.index <= grab_time, "ema50_slope"].iloc[-1]
            ema200     = df_4h.loc[df_4h.index <= grab_time, "ema200"].iloc[-1]
        except (IndexError, KeyError):
            seen_grabs[grab_key] = now_ts
            continue

        if direction == "long" and not (grab["close"] > ema200 or slope > 0):
            log.info(f"  [TREND] {sym_key} {grab_key} FAIL long: close={grab['close']:.0f} ema200={ema200:.0f} slope={slope:.3f} need>{cfg['slope_long']}")
            continue
        if direction == "short" and not (grab["close"] < ema200 or slope < 0):
            log.info(f"  [TREND] {sym_key} {grab_key} FAIL short: close={grab['close']:.0f} ema200={ema200:.0f} slope={slope:.3f}")
        
            continue

        # Find BOS on 15M
        try:
            m15_start = df_15m.index.searchsorted(grab_time)
        except Exception:
            seen_grabs[grab_key] = now_ts; continue
        entry_window = cfg.get("entry_window", ENTRY_WINDOW)

        if m15_start >= len(df_15m) - entry_window:
            continue

        bos_idx = detect_bos(df_15m, m15_start, direction, window=entry_window)
        if bos_idx is None:
            log.info(f"  [DEBUG] {sym_key} grab={grab_key} BOS not found in window={entry_window}")
            continue

        # ─── Volume Spike Confirmation ────────────────────────────────
        try:
            vol_series = df_15m['volume']
            avg_vol = vol_series.iloc[max(0, bos_idx-20):bos_idx].mean()
            vol_spike = vol_series.iloc[bos_idx] > avg_vol * VOLUME_SPIKE
        except Exception:
            vol_spike = True

        if not vol_spike:
            log.info(f"  [DEBUG] {sym_key} grab={grab_key} BOS volume not significant – skip")
            seen_grabs[grab_key] = now_ts
            continue

        # ─── Find FVG after BOS ──────────────────────────────────────
        entry = sl = tp = None
        for j in range(bos_idx + 1, min(bos_idx + 8, len(df_15m) - 1)):
            fvg = detect_fvg(df_15m, j)
            if fvg is None:
                continue
            fvg_type, fvg_top, fvg_bot = fvg
            fvg_mid = (fvg_top + fvg_bot) / 2

            if direction == "long" and fvg_type == "bullish":
                entry = fvg_mid
                # Compute ATR-based SL
                atr = calculate_atr(df_15m)
                sl = round(entry - atr * ATR_MULTIPLIER, 2)
                risk = entry - sl
                if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                    continue
                # Dynamic RR based on ADX
                adx = calculate_adx(df_4h)
                if adx > ADX_STRONG:
                    rr = 4
                elif adx > ADX_MODERATE:
                    rr = 3
                else:
                    rr = 2
                tp = entry + risk * rr
                break

            elif direction == "short" and fvg_type == "bearish":
                entry = fvg_mid
                atr = calculate_atr(df_15m)
                sl = round(entry + atr * ATR_MULTIPLIER, 2)
                risk = sl - entry
                if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                    continue
                adx = calculate_adx(df_4h)
                if adx > ADX_STRONG:
                    rr = 4
                elif adx > ADX_MODERATE:
                    rr = 3
                else:
                    rr = 2
                tp = entry - risk * rr
                break

        seen_grabs[grab_key] = now_ts

        if entry is None:
            continue

        # ─── Live price proximity check ──────────────────────────────
        ticker = ex.fetch_ticker(SYMBOL_MAP[sym_key]["ccxt"])
        current_price = ticker['last']
        price_diff_pct = abs(current_price - entry) / entry
        log.info(f"  [GATE] entry={entry:.2f} live={current_price:.2f} diff={price_diff_pct*100:.2f}% risk_pct={(abs(entry-sl)/entry)*100:.2f}%")
        

        if price_diff_pct > PRICE_PROXIMITY:
            log.info(f"  Live price {current_price:.2f} is {price_diff_pct*100:.2f}% away from FVG entry {entry:.2f} – waiting for retest")
            continue
        log.info(f"  Live price {current_price:.2f} is within {price_diff_pct*100:.2f}% of FVG – ready to fire")

        log.info(f"  SIGNAL: {sym_key} {direction.upper()} | entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} (RR=1:{rr})")
        sig_msg = (
            f"🔍 <b>SIGNAL</b>: {sym_key} {direction.upper()}\n"
            f"Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f} (RR 1:{rr})"
        )
        tg(sig_msg)
        return direction, entry, sl, tp

    return None

# ─── POSITION MONITOR ────────────────────────────────────────────────────────

class PosTracker:
    def __init__(self, daily_trades: dict, daily_loss: dict):
        self.open = {}
        self._logged = set()
        self._daily_trades = daily_trades
        self._daily_loss = daily_loss
        self._lock = threading.Lock()

    def _fetch_exit_price(self, ex, m):
        ccxt_sym = m["ccxt_sym"]
        close_side = "sell" if m["side"] == "buy" else "buy"
        opened_ts = int(m["opened_at"].timestamp() * 1000)

        try:
            trades = ex.fetch_my_trades(ccxt_sym, since=opened_ts, limit=20)
            close_fills = [t for t in trades if t.get("side") == close_side and float(t.get("amount", 0)) > 0]
            if close_fills:
                close_fills.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
                return float(close_fills[0]["price"])
        except Exception:
            pass

        try:
            orders = ex.fetch_closed_orders(ccxt_sym, since=opened_ts, limit=20)
            close_orders = [
                o for o in orders
                if o.get("side") == close_side
                and o.get("status") in ("closed", "filled")
                and float(o.get("average") or o.get("price") or 0) > 0
            ]
            if close_orders:
                close_orders.sort(key=lambda o: o.get("timestamp", 0), reverse=True)
                return float(close_orders[0].get("average") or close_orders[0]["price"])
        except Exception:
            pass

        try:
            px = float(ex.fetch_ticker(ccxt_sym)["last"])
            if m.get("side") == "buy":
                if m.get("sl") and px <= m["sl"]: return m["sl"]
                if m.get("tp") and px >= m["tp"]: return m["tp"]
            else:
                if m.get("sl") and px >= m["sl"]: return m["sl"]
                if m.get("tp") and px <= m["tp"]: return m["tp"]
            return px
        except Exception:
            return m["entry"]

    def add(self, sym_key, meta):
        # Add partial TP level (50% of position at 1:2 RR)
        risk = abs(meta['entry'] - meta['sl'])
        rr_partial = 2.0  # fixed for partial
        meta['tp_partial'] = meta['entry'] + risk * rr_partial if meta['side'] == 'buy' else meta['entry'] - risk * rr_partial
        meta['partial_filled'] = False
        with self._lock:
            self.open[sym_key] = meta
            save_positions(self.open)

    def remove(self, sym_key):
        with self._lock:
            if sym_key in self.open:
                del self.open[sym_key]
                save_positions(self.open)

    def check_and_log(self, ex, sym_key):
        with self._lock:
            if sym_key not in self.open:
                return
            m = self.open[sym_key]
            ccxt_sym = m["ccxt_sym"]
        
        if has_open_position(ex, ccxt_sym):
            try:
                px = float(ex.fetch_ticker(ccxt_sym)["last"])
                sign = 1 if m["side"] == "buy" else -1
                upnl = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
                log.info(f"  {sym_key} {m['side'].upper()} open | px={px} uPnL={upnl:+.4f} USD")

                # ── Partial TP check ──────────────────────────────────
                if not m.get('partial_filled') and m.get('tp_partial'):
                    if (m['side'] == 'buy' and px >= m['tp_partial']) or (m['side'] == 'sell' and px <= m['tp_partial']):
                        half_lots = max(1, round(m['lots'] / 2, 0))
                        if half_lots > 0 and m['lots'] > 1:
                            close_side = 'sell' if m['side'] == 'buy' else 'buy'
                            try:
                                ex.create_order(ccxt_sym, 'market', close_side, half_lots, params={'reduce_only': True})
                                with self._lock:
                                    m['lots'] = m['lots'] - half_lots
                                    m['partial_filled'] = True
                                    save_positions(self.open)
                                tg(f"📊 <b>Partial TP hit</b>: {sym_key} closed {half_lots} lots at {px:.2f}")
                            except Exception as e:
                                log.warning(f"Partial TP order failed: {e}")

                # ── Trailing SL ──────────────────────────────────────
                trail_dist = m.get("trail_dist")
                if trail_dist:
                    new_sl = None
                    if m["side"] == "buy":
                        with self._lock:
                            if "highest_px" not in m:
                                m["highest_px"] = max(m["entry"], px)
                            if px > m["highest_px"]:
                                m["highest_px"] = px
                                candidate = round(px - trail_dist, 2)
                                if candidate > m["sl"]:
                                    m["sl"] = candidate
                                    new_sl = candidate
                                    save_positions(self.open)
                        if new_sl is not None:
                            try:
                                open_orders = ex.fetch_open_orders(ccxt_sym)
                                for o in open_orders:
                                    if o.get("type") == "stop" and (o.get("reduceOnly") or o.get("info", {}).get("reduce_only")):
                                        ex.cancel_order(o["id"], ccxt_sym)
                                ex.create_order(ccxt_sym, "stop", "sell", m["lots"],
                                                params={"stopPrice": str(new_sl), "reduce_only": True})
                                tg(f"ℹ️ <b>Stop Loss Trailed</b>: {sym_key}\nNew SL: {new_sl:.2f} (Price: {px:.2f})")
                            except Exception as err:
                                log.error(f"Trailing SL update failed: {err}")
                    else:
                        with self._lock:
                            if "lowest_px" not in m:
                                m["lowest_px"] = min(m["entry"], px)
                            if px < m["lowest_px"]:
                                m["lowest_px"] = px
                                candidate = round(px + trail_dist, 2)
                                if candidate < m["sl"]:
                                    m["sl"] = candidate
                                    new_sl = candidate
                                    save_positions(self.open)
                        if new_sl is not None:
                            try:
                                open_orders = ex.fetch_open_orders(ccxt_sym)
                                for o in open_orders:
                                    if o.get("type") == "stop" and (o.get("reduceOnly") or o.get("info", {}).get("reduce_only")):
                                        ex.cancel_order(o["id"], ccxt_sym)
                                ex.create_order(ccxt_sym, "stop", "buy", m["lots"],
                                                params={"stopPrice": str(new_sl), "reduce_only": True})
                                tg(f"ℹ️ <b>Stop Loss Trailed</b>: {sym_key}\nNew SL: {new_sl:.2f} (Price: {px:.2f})")
                            except Exception as err:
                                log.error(f"Trailing SL update failed: {err}")
            except Exception as e:
                log.warning(f"Error in position check: {e}")
            return

        # Position closed – log it
        with self._lock:
            trade_key = f"{sym_key}_{m['opened_at'].isoformat()}"
            if trade_key in self._logged:
                log.info(f"  {sym_key}: already logged.")
                if sym_key in self.open:
                    del self.open[sym_key]
                    save_positions(self.open)
                return

        now = datetime.now(timezone.utc)
        px = self._fetch_exit_price(ex, m)

        sign = 1 if m["side"] == "buy" else -1
        pnl = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
        if pnl < 0:
            self._daily_loss[now.date()] = self._daily_loss.get(now.date(), 0) + abs(pnl)
        save_daily(self._daily_trades, self._daily_loss)
        hold = round((now - m["opened_at"]).total_seconds() / 60, 1)

        row = {
            "date": now.strftime("%Y-%m-%d"),
            "symbol": m["ccxt_sym"],
            "side": m["side"],
            "lots": m["lots"],
            "contract_size": m["contract_size"],
            "entry_price": round(m["entry"], 4),
            "exit_price": round(px, 4),
            "sl_price": round(m["sl"], 4),
            "tp_price": round(m["tp"], 4),
            "pnl_usd": round(pnl, 4),
            "result": "win" if pnl >= 0 else "loss",
            "hold_time_min": hold,
            "opened_at": m["opened_at"].strftime("%Y-%m-%d %H:%M:%S UTC"),
            "closed_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        log.info(f"  CLOSED {sym_key}: {'WIN' if pnl>=0 else 'LOSS'}  PnL={pnl:+.4f} USD  exit={px:.2f}")
        emoji = "🟢" if pnl >= 0 else "🔴"
        tg(f"{emoji} <b>CLOSED</b>: {sym_key} {m['side'].upper()}\nPnL: {pnl:+.4f} USD | Exit: {px:.2f}\nHold: {hold}min | {'WIN' if pnl>=0 else 'LOSS'}")
        append_csv(row)
        with self._lock:
            self._logged.add(trade_key)
            if sym_key in self.open:
                del self.open[sym_key]
                save_positions(self.open)

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    log.info("="*55)
    log.info("  LIVE PAPER TRADER — Delta India Demo")
    log.info("  Strategy: 4H Liquidity Grab + 15M SMC Entry")
    log.info("  Improvements: ATR SL, Partial TP, ADX RR, Volume confirm")
    log.info("="*55)

    ex = make_exchange()
    ex.load_markets()
    init_csv()

    seen_grabs = load_seen_grabs()
    daily_trades, daily_loss = load_daily()
    tracker = PosTracker(daily_trades, daily_loss)

    # Restore positions
    restored = load_positions()
    if restored:
        log.info(f"\nRestoring {len(restored)} position(s) from last session...")
        cleaned = reconcile_positions(restored, ex, append_csv, tracker._logged)
        tracker.open = cleaned if cleaned else restored
        save_positions(tracker.open)
        if tracker.open:
            tg(f"🔄 <b>Bot restarted</b>. Restored {len(tracker.open)} open position(s): {', '.join(tracker.open.keys())}")
        else:
            tg("🔄 <b>Bot restarted</b>. All positions closed while offline.")
    else:
        tg("🔄 <b>Bot restarted</b>. No previous positions found.")

    kill_switch = threading.Event()
    start_bot(tracker, daily_trades, daily_loss, ex, kill_switch)

    threading.Thread(target=_health_ping_loop, args=(tracker, daily_trades, daily_loss, ex, kill_switch), daemon=True).start()
    log.info("[HEALTH] Ping thread started.")

    # Warm start
    if seen_grabs:
        log.info(f"\nWarm-start skipped — {len(seen_grabs)} grab keys restored.")
    else:
        log.info("\nWarm-start: scanning historical grabs (will NOT trade these)...")
        now_ts = datetime.now(timezone.utc).timestamp()
        for sym_key, info in SYMBOL_MAP.items():
            df_4h = retry(fetch_candles, ex, info["ccxt"], "4h", CANDLES_4H)
            if df_4h is None or len(df_4h) < 50:
                continue
            df_4h = add_emas(df_4h.copy())
            cfg = SYMBOL_CONFIG.get(sym_key, SYMBOL_CONFIG["ETHUSDT"])
            grabs = detect_liquidity_grabs(df_4h, min_wick_pct=cfg["min_wick_pct"])
            for _, g in grabs.iterrows():
                grab_age = (now_ts - g["grab_time"].timestamp()) / 3600
                if grab_age > 48:
                    seen_grabs[str(g["grab_time"])] = now_ts
            log.info(f"  {sym_key}: {len(grabs)} existing grabs marked seen")
        save_seen_grabs(seen_grabs)

    log.info("\nWarm-start done. Watching for NEW signals only.\n")
    fetch_failures = {}
    broadcast_seen, broadcast_seen_dict = _load_broadcast_seen()
    log.info(f"Loaded {len(broadcast_seen)} broadcast-seen entries from disk.")

    # Main loop
    pending_orders = {}
    tracker.pending_orders = pending_orders

    while True:
        time.sleep(POLL_INTERVAL)
        now = datetime.now(timezone.utc)
        day_key = now.date()
        equity = get_balance(ex)

        log.info(f"\n--{now.strftime('%Y-%m-%d %H:%M')} UTC  equity={equity:.2f} USD --")

        # ── Check pending limit orders ──
        for sym_key in list(pending_orders.keys()):
            data = pending_orders[sym_key]
            try:
                order = ex.fetch_order(data['id'], data['ccxt_sym'])
                if order['status'] in ('closed', 'filled'):
                    actual_entry = float(order.get('average') or order.get('price') or data['entry'])
                    opened_at = datetime.now(timezone.utc)
                    tracker.add(sym_key, {
                        "ccxt_sym": data['ccxt_sym'],
                        "side": data['side'],
                        "lots": data['lots'],
                        "entry": actual_entry,
                        "sl": data['sl'],
                        "tp": data['tp'],
                        "opened_at": opened_at,
                        "contract_size": data['contract_size'],
                        "trail_dist": round(abs(actual_entry - data['sl']) * 0.5, 2),
                    })
                    daily_trades[day_key] = daily_trades.get(day_key, 0) + 1
                    save_daily(daily_trades, daily_loss)
                    tg(f"✅ <b>LIMIT ORDER FILLED</b>: {sym_key} {data['side'].upper()} x{data['lots']}\nEntry: {actual_entry:.2f}")
                    del pending_orders[sym_key]
                elif time.time() - data['ts'] > ORDER_TIMEOUT:
                    ex.cancel_order(data['id'], data['ccxt_sym'])
                    tg(f"⏰ Cancelled stale limit order for {sym_key}")
                    del pending_orders[sym_key]
            except Exception as e:
                log.warning(f"Pending order check error for {sym_key}: {e}")

        # ── Check open positions ──
        with tracker._lock:
            sym_keys = list(tracker.open.keys())
        for sym_key in sym_keys:
            tracker.check_and_log(ex, sym_key)

        # ── Kill switch & daily limits (commented out) ──
        if kill_switch.is_set():
            log.info("Kill switch active. Skipping.")
            continue

        # ── Scan each symbol ──
        for sym_key, info in SYMBOL_MAP.items():
            ccxt_sym = info["ccxt"]
            contract_size = info["contract_size"]

            with tracker._lock:
                if sym_key in tracker.open:
                    continue
            if sym_key in pending_orders:
                log.info(f"  {sym_key}: pending limit order, skip scan.")
                continue
            if has_open_position(ex, ccxt_sym):
                log.info(f"  {sym_key}: position already open, skip.")
                continue

            df_4h = retry(fetch_candles, ex, ccxt_sym, "4h", CANDLES_4H)
            df_15m = retry(fetch_candles, ex, ccxt_sym, "15m", CANDLES_15M)
            if df_4h is None or df_15m is None:
                fetch_failures[sym_key] = fetch_failures.get(sym_key, 0) + 1
                if fetch_failures[sym_key] == 3:
                    tg(f"⚠️ Fetch Alert: {sym_key} failed 3 times.")
                    log.warning(f"  {sym_key}: 3 consecutive fetch failures")
                continue
            if len(df_4h) < 50 or len(df_15m) < 100:
                continue
            fetch_failures[sym_key] = 0

            signal = find_fresh_signal(sym_key, df_4h, df_15m, seen_grabs, equity, ex)
            if signal is None:
                # Fallback 1: EMA retracement entry (continuation in trend)
                result = find_ema_retracement_signal(sym_key, df_4h, df_15m, ex)
                if result:
                    direction, entry, sl, tp = result
                    signal = (direction, entry, sl, tp)
                    log.info(f"  {sym_key}: EMA retracement signal")

            if signal is None:
                # Fallback 2: Continuation after fresh 15M BOS + FVG (no grab needed)
                result = find_continuation_signal(sym_key, df_4h, df_15m, ex)
                if result:
                    direction, entry, sl, tp = result
                    signal = (direction, entry, sl, tp)
                    log.info(f"  {sym_key}: Continuation signal")

            if signal is None:
                log.info(f"  {sym_key}: no signal.")
                save_seen_grabs(seen_grabs)
                continue

            direction, entry, sl, tp = signal
            side = "buy" if direction == "long" else "sell"
            lots = calc_lots(equity, entry, sl, contract_size)
            if lots < 1:
                log.info(f"  {sym_key}: lot size < 1, skip.")
                continue

            rr   = round(abs(tp - entry) / abs(entry - sl), 1) if abs(entry - sl) > 0 else 2.0

            # Deduplicate: only broadcast each signal once (survives restarts)
            _sig_id = f"{sym_key}_{side}_{round(entry, 0)}_{round(sl, 0)}"
            if _sig_id in broadcast_seen:
                log.info(f"  {sym_key}: signal already broadcast ({_sig_id}), skipping.")
            else:
                # Broadcast signal to channels and send Telegram alert
                try:
                    from bot_server import send_crypto_signal
                    send_crypto_signal(sym_key, side, lots, entry, sl, tp, rr, 10, contract_size)
                except Exception as e:
                    log.warning(f"  broadcast failed: {e}")
                    tg(f"🔍 <b>SIGNAL</b>: {sym_key} {side.upper()}\nEntry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f} (RR 1:{rr})")
                # Mark as broadcast
                broadcast_seen.add(_sig_id)
                broadcast_seen_dict[_sig_id] = time.time()
                _save_broadcast_seen(broadcast_seen_dict)

            log.info(f"  ENTERING {sym_key} {side.upper()} {lots}L | entry~{entry:.2f} SL={sl:.2f} TP={tp:.2f}")
            try:
                order = place_bracket_limit(ex, ccxt_sym, side, lots, entry, sl, tp)
                pending_orders[sym_key] = {
                    'id': order['id'],
                    'ts': time.time(),
                    'entry': entry,
                    'sl': sl,
                    'tp': tp,
                    'side': side,
                    'lots': lots,
                    'ccxt_sym': ccxt_sym,
                    'contract_size': contract_size,
                }
                log.info(f"  LIMIT ORDER PLACED (pending) id={order['id']}")
                tg(f"📌 <b>LIMIT ORDER PLACED</b>: {sym_key} {side.upper()} x{lots} @ {entry:.2f}\nSL: {sl:.2f} | TP: {tp:.2f}")
            except Exception as e:
                log.error(f"  Place order failed: {e}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\nStopped by user.")