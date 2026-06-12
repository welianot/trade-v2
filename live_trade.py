"""
live_trade.py
=============
Live paper-trading runner for Delta India Demo.
Strategy: 4H Liquidity Grab + 15M SMC Entry (BOS + FVG)

Reuses detection logic from back_test.py.

Run:
    python live_trade.py

Behavior:
  - Warm-starts: marks all historical grabs as seen (no stale trades).
  - Every 15m candle close: scans for fresh signal on each symbol.
  - On signal: places market order + bracket SL/TP on demo.
  - One open trade per symbol at a time.
  - Respects daily trade + loss limits from back_test config.
  - Appends results to trades_log.csv.
  - Persists open positions + daily state to JSON — survives restarts.
"""

import ccxt
import csv
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
from back_test import (
    add_emas, detect_liquidity_grabs, detect_bos, detect_fvg,
    SYMBOL_CONFIG, MIN_RR, RISK_PER_TRADE, ACCOUNT_SIZE,
    SWING_LOOKBACK, ENTRY_WINDOW, MAX_TRADES_PER_DAY, DAILY_LOSS_LIMIT,
)
# ─── Import strategy logic ──────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from back_test import (
    add_emas,
    detect_liquidity_grabs,
    detect_bos,
    detect_fvg,
    SYMBOL_CONFIG,
    MIN_RR,
    RISK_PER_TRADE,
    ACCOUNT_SIZE,
    SWING_LOOKBACK,
    ENTRY_WINDOW,
    MAX_TRADES_PER_DAY,
    DAILY_LOSS_LIMIT,
)

# ─── CONFIG ─────────────────────────────────────────────────────────────────

DEMO_HOST    = "https://cdn-ind.testnet.deltaex.org"
LOG_FILE     = "trades_log.csv"
POLL_SECONDS = 60    # how often to check inside a 15m window
CANDLES_4H   = 300   # lookback for 4H fetch
CANDLES_15M  = 800   # lookback for 15M fetch

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
    ex = ccxt.delta({"apiKey": key or "", "secret": sec or "", "enableRateLimit": True})
    ex.urls = ex.urls or {}
    ex.urls["api"] = {"public": DEMO_HOST, "private": DEMO_HOST}
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
        raw = ex.fetch_ohlcv(ccxt_sym, timeframe, limit=limit)
        df  = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
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


def place_bracket(ex, ccxt_sym, side, lots, sl, tp):
    close_side = "sell" if side == "buy" else "buy"
    order = ex.create_order(
        ccxt_sym, "market", side, lots,
        params={
            "bracket_stop_loss_price":        str(round(sl, 2)),
            "bracket_stop_loss_limit_price":  str(round(sl, 2)),
            "bracket_take_profit_price":      str(round(tp, 2)),
            "bracket_take_profit_limit_price":str(round(tp, 2)),
        },
    )
    return order


def seconds_to_next_15m():
    now = datetime.now(timezone.utc)
    mins_past = now.minute % 15
    secs_past = mins_past * 60 + now.second
    wait = (15 * 60) - secs_past + 5   # +5s buffer for candle to settle
    return max(wait, 10)


# ─── HEALTH PING ─────────────────────────────────────────────────────────────

def _health_ping_loop(tracker, daily_trades, daily_loss, ex, kill_switch):
    """Background thread — sends Telegram status every 1h."""
    PING_INTERVAL = 3600  # seconds
    while True:
        time.sleep(PING_INTERVAL)
        try:
            equity = get_balance(ex)
            day_key = datetime.now(timezone.utc).date()
            trades_today = daily_trades.get(day_key, 0)
            loss_today   = daily_loss.get(day_key, 0.0)
            
            # Take snapshot of open positions under lock to avoid race condition
            with tracker._lock:
                open_count = len(tracker.open)
                open_snapshot = dict(tracker.open)
            
            paused       = "⏸ PAUSED" if kill_switch.is_set() else "▶ RUNNING"

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
    """Remove grabs older than `days` days from seen_grabs dict."""
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts = now_ts - (days * 86400)
    stale_keys = [k for k, ts in seen_grabs.items() if isinstance(ts, (int, float)) and ts < cutoff_ts]
    for k in stale_keys:
        del seen_grabs[k]
    if stale_keys:
        log.info(f"  Pruned {len(stale_keys)} old grab keys (>7 days)")
    return seen_grabs
def find_fresh_signal(sym_key, df_4h, df_15m, seen_grabs, equity):
    """
    Returns (direction, entry, sl, tp) if fresh signal found, else None.
    Marks grab as seen either way (prevents re-triggering next loop).
    seen_grabs is now a dict: {grab_key: timestamp}
    """
    # Prune old grabs on each scan (once per symbol per 15m candle)
    prune_seen_grabs(seen_grabs, days=7)
    
    cfg = SYMBOL_CONFIG.get(sym_key, SYMBOL_CONFIG["ETHUSDT"])

    df_4h = add_emas(df_4h.copy())
    grabs = detect_liquidity_grabs(df_4h, min_wick_pct=cfg["min_wick_pct"])
    if grabs.empty:
        return None

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    now_ts = datetime.now(timezone.utc).timestamp()

    for _, grab in grabs.iloc[::-1].iterrows():   # newest first
        grab_key  = str(grab["grab_time"])
        grab_type = grab["grab_type"]
        direction = "long" if grab_type == "bullish" else "short"
        grab_time = grab["grab_time"]

        # Only consider grabs within the active entry window (last 4h)
        age_hours = (now_utc - grab_time).total_seconds() / 3600
        if age_hours > 4:
            seen_grabs[grab_key] = now_ts   # too old, mark done with timestamp
            continue

        if grab_key in seen_grabs:
            continue

        # Session filter
        if grab_time.hour not in cfg["session_hours"]:
            seen_grabs[grab_key] = now_ts
            continue

        # Trend filter
        try:
            slope      = df_4h.loc[df_4h.index <= grab_time, "ema50_slope"].iloc[-1]
            ema200     = df_4h.loc[df_4h.index <= grab_time, "ema200"].iloc[-1]
        except (IndexError, KeyError):
            seen_grabs[grab_key] = now_ts
            continue

        if direction == "long"  and grab["close"] < ema200 and slope < cfg["slope_long"]:
            seen_grabs[grab_key] = now_ts; continue
        if direction == "short" and grab["close"] > ema200 and slope > abs(cfg["slope_short"]):
            seen_grabs[grab_key] = now_ts; continue

        # Find BOS on 15M
        try:
            m15_start = df_15m.index.searchsorted(grab_time)
        except Exception:
            seen_grabs[grab_key] = now_ts; continue

        if m15_start >= len(df_15m) - ENTRY_WINDOW:
            continue

        bos_idx = detect_bos(df_15m, m15_start, direction, window=ENTRY_WINDOW)
        if bos_idx is None:
            continue

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
                sl    = grab["low"] * 0.999
                risk  = entry - sl
                if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                    continue
                tp = entry + risk * MIN_RR
                break

            elif direction == "short" and fvg_type == "bearish":
                entry = fvg_mid
                sl    = grab["high"] * 1.001
                risk  = sl - entry
                if risk <= 0 or risk / entry > cfg["max_risk_pct"]:
                    continue
                tp = entry - risk * MIN_RR
                break

        seen_grabs[grab_key] = now_ts  # mark regardless of outcome with timestamp

        if entry is None:
            continue

        # Only fire if FVG entry candle is recent (within last 3 15m candles)
        if bos_idx < len(df_15m) - 10:
            log.info(f"  Signal formed but stale (bos_idx={bos_idx}, len={len(df_15m)}). Skip.")
            continue

        log.info(f"  SIGNAL: {sym_key} {direction.upper()} | entry={entry:.2f} SL={sl:.2f} TP={tp:.2f}")
        tg(f"🔍 <b>SIGNAL</b>: {sym_key} {direction.upper()}\nEntry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
        return direction, entry, sl, tp

    return None


# ─── POSITION MONITOR ────────────────────────────────────────────────────────

class PosTracker:
    """Track open trade metadata per symbol."""
    def __init__(self, daily_trades: dict, daily_loss: dict):
        self.open = {}   # sym_key → {ccxt_sym, side, lots, entry, sl, tp, opened_at, contract_size}
        self._logged = set()  # track logged trades to prevent duplicates
        self._daily_trades = daily_trades
        self._daily_loss = daily_loss
        self._lock = threading.Lock()  # protects self.open from concurrent access (Telegram + main loop)

    def _fetch_exit_price(self, ex, m):
        """Fetch the actual fill price of the closing order, not the live ticker."""
        ccxt_sym = m["ccxt_sym"]
        close_side = "sell" if m["side"] == "buy" else "buy"
        opened_ts = int(m["opened_at"].timestamp() * 1000)

        # Method 1: fetch recent trades for actual fill prices
        try:
            trades = ex.fetch_my_trades(ccxt_sym, since=opened_ts, limit=20)
            close_fills = [
                t for t in trades
                if t.get("side") == close_side and float(t.get("amount", 0)) > 0
            ]
            if close_fills:
                close_fills.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
                px = float(close_fills[0]["price"])
                log.info(f"    Exit price from trades: {px}")
                return px
        except Exception as e:
            log.debug(f"    fetch_my_trades failed: {e}")

        # Method 2: fetch closed orders
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
                o = close_orders[0]
                px = float(o.get("average") or o["price"])
                log.info(f"    Exit price from orders: {px}")
                return px
        except Exception as e:
            log.debug(f"    fetch_closed_orders failed: {e}")

        # Method 3: fallback to ticker (legacy behavior, inaccurate)
        try:
            px = float(ex.fetch_ticker(ccxt_sym)["last"])
            log.warning(f"    Using ticker as exit price (may be inaccurate): {px}")
            if m.get("side") == "buy":
                if m.get("sl") and px <= m["sl"]:
                    return m["sl"]
                if m.get("tp") and px >= m["tp"]:
                    return m["tp"]
            else:
                if m.get("sl") and px >= m["sl"]:
                    return m["sl"]
                if m.get("tp") and px <= m["tp"]:
                    return m["tp"]
            return px
        except Exception:
            log.warning("    Could not fetch exit price, using entry as fallback")
            return m["entry"]

    def add(self, sym_key, meta):
        with self._lock:
            self.open[sym_key] = meta
            save_positions(self.open)          # ← persist

    def remove(self, sym_key):
        """Remove from tracker and persist state. Must be called WITHOUT holding self._lock."""
        with self._lock:
            if sym_key in self.open:
                del self.open[sym_key]
                save_positions(self.open)      # ← persist

    def check_and_log(self, ex, sym_key):
        with self._lock:
            if sym_key not in self.open:
                return
            m        = self.open[sym_key]
            ccxt_sym = m["ccxt_sym"]
        
        if has_open_position(ex, ccxt_sym):
            try:
                px   = float(ex.fetch_ticker(ccxt_sym)["last"])
                sign = 1 if m["side"] == "buy" else -1
                upnl = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
                log.info(f"  {sym_key} {m['side'].upper()} open | px={px} uPnL={upnl:+.4f} USD")
                
                # Trailing SL calculation and execution
                trail_dist = m.get("trail_dist")
                if trail_dist:
                    if m["side"] == "buy":
                        if "highest_px" not in m:
                            m["highest_px"] = max(m["entry"], px)
                        if px > m["highest_px"]:
                            m["highest_px"] = px
                            new_sl = round(px - trail_dist, 2)
                            if new_sl > m["sl"]:
                                m["sl"] = new_sl
                                with self._lock:
                                    save_positions(self.open)  # ← persist updated SL
                                # Update SL order on exchange
                                try:
                                    open_orders = ex.fetch_open_orders(ccxt_sym)
                                    for o in open_orders:
                                        if o.get("type") == "stop" and (o.get("reduceOnly") or o.get("info", {}).get("reduce_only")):
                                            ex.cancel_order(o["id"], ccxt_sym)
                                    ex.create_order(
                                        ccxt_sym, "stop", "sell", m["lots"],
                                        params={"stopPrice": str(new_sl), "reduce_only": True}
                                    )
                                    tg(f"ℹ️ <b>Stop Loss Trailed</b>: {sym_key}\nNew SL: {new_sl:.2f} (Price: {px:.2f})")
                                except Exception as err:
                                    log.error(f"Trailing SL update failed: {err}")
                    else:
                        if "lowest_px" not in m:
                            m["lowest_px"] = min(m["entry"], px)
                        if px < m["lowest_px"]:
                            m["lowest_px"] = px
                            new_sl = round(px + trail_dist, 2)
                            if new_sl < m["sl"]:
                                m["sl"] = new_sl
                                with self._lock:
                                    save_positions(self.open)  # ← persist updated SL
                                # Update SL order on exchange
                                try:
                                    open_orders = ex.fetch_open_orders(ccxt_sym)
                                    for o in open_orders:
                                        if o.get("type") == "stop" and (o.get("reduceOnly") or o.get("info", {}).get("reduce_only")):
                                            ex.cancel_order(o["id"], ccxt_sym)
                                    ex.create_order(
                                        ccxt_sym, "stop", "buy", m["lots"],
                                        params={"stopPrice": str(new_sl), "reduce_only": True}
                                    )
                                    tg(f"ℹ️ <b>Stop Loss Trailed</b>: {sym_key}\nNew SL: {new_sl:.2f} (Price: {px:.2f})")
                                except Exception as err:
                                    log.error(f"Trailing SL update failed: {err}")
            except Exception as e:
                log.warning(f"Error in trailing SL check: {e}")
            return

        # Position closed — log it
        with self._lock:
            trade_key = f"{sym_key}_{m['opened_at'].isoformat()}"
            if trade_key in self._logged:
                log.info(f"  {sym_key}: already logged, skipping duplicate.")
                if sym_key in self.open:
                    del self.open[sym_key]
                save_positions(self.open)      # ← persist directly
                return

        now = datetime.now(timezone.utc)

        # Fetch actual fill price from exchange (not ticker)
        px = self._fetch_exit_price(ex, m)

        sign = 1 if m["side"] == "buy" else -1
        pnl  = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
        if pnl < 0:
            self._daily_loss[now.date()] = self._daily_loss.get(now.date(), 0) + abs(pnl)
        save_daily(self._daily_trades, self._daily_loss)  # ← persist daily loss & trades with instance refs
        hold = round((now - m["opened_at"]).total_seconds() / 60, 1)

        row = {
            "date":          now.strftime("%Y-%m-%d"),
            "symbol":        m["ccxt_sym"],
            "side":          m["side"],
            "lots":          m["lots"],
            "contract_size": m["contract_size"],
            "entry_price":   round(m["entry"], 4),
            "exit_price":    round(px, 4),
            "sl_price":      round(m["sl"], 4),
            "tp_price":      round(m["tp"], 4),
            "pnl_usd":       round(pnl, 4),
            "result":        "win" if pnl >= 0 else "loss",
            "hold_time_min": hold,
            "opened_at":     m["opened_at"].strftime("%Y-%m-%d %H:%M:%S UTC"),
            "closed_at":     now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        log.info(f"  CLOSED {sym_key}: {'WIN' if pnl>=0 else 'LOSS'}  PnL={pnl:+.4f} USD  exit={px:.2f}")
        emoji = "🟢" if pnl >= 0 else "🔴"
        tg(f"{emoji} <b>CLOSED</b>: {sym_key} {m['side'].upper()}\nPnL: {pnl:+.4f} USD | Exit: {px:.2f}\nHold: {hold}min | {'WIN' if pnl>=0 else 'LOSS'}")
        append_csv(row)
        # Finalize: mark logged and remove from open positions (without calling remove() inside lock to avoid deadlock)
        with self._lock:
            self._logged.add(trade_key)
            if sym_key in self.open:
                del self.open[sym_key]
            save_positions(self.open)          # ← persist directly, no remove() call


# (REMOVED: daily_trades_ref global) — PosTracker now stores instance refs via __init__


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    log.info("="*55)
    log.info("  LIVE PAPER TRADER — Delta India Demo")
    log.info("  Strategy: 4H Liquidity Grab + 15M SMC Entry")
    log.info("="*55)

    ex = make_exchange()
    ex.load_markets()
    init_csv()

    seen_grabs   = load_seen_grabs()   # #5: restore from disk, skip re-scan if populated

    # ── Restore daily state ──────────────────────────────────────────────────
    daily_trades, daily_loss = load_daily()
    tracker = PosTracker(daily_trades, daily_loss)

    # ── Restore open positions ───────────────────────────────────────────────
    restored = load_positions()
    if restored:
        log.info(f"\nRestoring {len(restored)} position(s) from last session...")
        cleaned = reconcile_positions(restored, ex, append_csv, tracker._logged)
        tracker.open = cleaned if cleaned else restored
        save_positions(tracker.open)
        if tracker.open:
            tg(f"🔄 <b>Bot restarted</b>. Restored {len(tracker.open)} open position(s): {', '.join(tracker.open.keys())}")
        else:
            tg("🔄 <b>Bot restarted</b>. All positions were closed while offline — logged to CSV.")
    else:
        tg("🔄 <b>Bot restarted</b>. No previous positions found.")

    kill_switch  = threading.Event()

    # ── Start Telegram bot thread ────────────────────────────────────────────
    start_bot(tracker, daily_trades, daily_loss, ex, kill_switch)

    # ── Start health ping thread (#8) ────────────────────────────────────────
    threading.Thread(
        target=_health_ping_loop,
        args=(tracker, daily_trades, daily_loss, ex, kill_switch),
        name="HealthPing",
        daemon=True,
    ).start()
    log.info("[HEALTH] Ping thread started (every 1h).")

    # ── Warm start: mark all existing historical grabs as seen ──────────────
    if seen_grabs:
        log.info(f"\nWarm-start skipped — {len(seen_grabs)} grab keys restored from disk.")
    else:
        log.info("\nWarm-start: scanning historical grabs (will NOT trade these)...")
        now_ts = datetime.now(timezone.utc).timestamp()
        for sym_key, info in SYMBOL_MAP.items():
            df_4h = fetch_candles(ex, info["ccxt"], "4h", CANDLES_4H)
            if df_4h is None or len(df_4h) < 50:
                continue
            df_4h = add_emas(df_4h.copy())
            cfg   = SYMBOL_CONFIG.get(sym_key, SYMBOL_CONFIG["ETHUSDT"])
            grabs = detect_liquidity_grabs(df_4h, min_wick_pct=cfg["min_wick_pct"])
            for _, g in grabs.iterrows():
                seen_grabs[str(g["grab_time"])] = now_ts  # store with current timestamp
            log.info(f"  {sym_key}: {len(grabs)} existing grabs marked seen")
        save_seen_grabs(seen_grabs)

    log.info("\nWarm-start done. Watching for NEW signals only.\n")
    fetch_failures = {}

    # ── Main loop ───────────────────────────────────────────────────────────
    while True:
        wait = seconds_to_next_15m()
        log.info(f"Next candle in {wait}s ...")
        time.sleep(wait)

        now      = datetime.now(timezone.utc)
        day_key  = now.date()
        equity   = get_balance(ex)

        log.info(f"\n--{now.strftime('%Y-%m-%d %H:%M')} UTC  equity={equity:.2f} USD --")

        # Check open positions
        with tracker._lock:
            sym_keys = list(tracker.open.keys())
        for sym_key in sym_keys:
            tracker.check_and_log(ex, sym_key)

        # Check kill switch (Telegram /pause)
        if kill_switch.is_set():
            log.info("Kill switch active (paused via Telegram). Skipping.")
            continue

        # Check daily limits
        if daily_trades.get(day_key, 0) >= MAX_TRADES_PER_DAY:
            log.info("Daily trade limit reached. Skipping signal scan.")
            tg("⚠️ Daily trade limit reached. No more entries today.")
            continue
        if daily_loss.get(day_key, 0) >= DAILY_LOSS_LIMIT * equity:
            log.info("Daily loss limit reached. Skipping signal scan.")
            tg(f"🛑 Daily loss limit hit. Bot paused for today.")
            continue

        # Scan each symbol
        for sym_key, info in SYMBOL_MAP.items():
            ccxt_sym      = info["ccxt"]
            contract_size = info["contract_size"]

            # Skip if position already open for this symbol
            with tracker._lock:
                if sym_key in tracker.open:
                    continue
            if has_open_position(ex, ccxt_sym):
                log.info(f"  {sym_key}: position already open, skip.")
                continue

            df_4h  = fetch_candles(ex, ccxt_sym, "4h",  CANDLES_4H)
            df_15m = fetch_candles(ex, ccxt_sym, "15m", CANDLES_15M)
            if df_4h is None or df_15m is None:
                fetch_failures[sym_key] = fetch_failures.get(sym_key, 0) + 1
                if fetch_failures[sym_key] == 3:
                    tg(f"⚠️ <b>Fetch Alert</b>: {sym_key} failed 3 consecutive times. Check connection.")
                    log.warning(f"  {sym_key}: 3 consecutive fetch failures")
                continue
            if len(df_4h) < 50 or len(df_15m) < 100:
                 continue
            fetch_failures[sym_key] = 0    

            signal = find_fresh_signal(sym_key, df_4h, df_15m, seen_grabs, equity)
            if signal is None:
                log.info(f"  {sym_key}: no signal.")
                save_seen_grabs(seen_grabs)   # #5: persist any newly-marked grabs
                continue

            direction, entry, sl, tp = signal
            side = "buy" if direction == "long" else "sell"
            lots = calc_lots(equity, entry, sl, contract_size)
            if lots < 1:
                log.info(f"  {sym_key}: lot size < 1, skip.")
                continue

            log.info(f"  ENTERING {sym_key} {side.upper()} {lots}L | entry~{entry:.2f} SL={sl:.2f} TP={tp:.2f}")
            try:
                order = place_bracket(ex, ccxt_sym, side, lots, sl, tp)
                opened_at = datetime.now(timezone.utc)
                actual_entry = float(order.get("average") or order.get("price") or entry)

                tracker.add(sym_key, {          # ← add() now auto-persists
                    "ccxt_sym":     ccxt_sym,
                    "side":         side,
                    "lots":         lots,
                    "entry":        actual_entry,
                    "sl":           sl,
                    "tp":           tp,
                    "opened_at":    opened_at,
                    "contract_size":contract_size,
                    "trail_dist":    round(abs(actual_entry - sl) * 0.5, 2),
                })
                daily_trades[day_key] = daily_trades.get(day_key, 0) + 1
                save_daily(daily_trades, daily_loss)  # ← persist trade count
                save_seen_grabs(seen_grabs)           # #5: persist newly-marked grabs
                log.info(f"  ORDER PLACED ✓ id={order.get('id')}  actual_entry={actual_entry}")
                tg(f"✅ <b>ORDER PLACED</b>: {sym_key} {side.upper()} x{lots}\nEntry: {actual_entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")

            except Exception as e:
                log.error(f"  Place order failed: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\nStopped by user.")