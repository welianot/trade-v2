"""
monitor_trade.py
================
Polls ALL open positions on Delta India Demo.
When a position closes (SL or TP hit), records the trade to trades_log.csv.

Run:
    python monitor_trade.py

Stops automatically when no positions remain open.
Poll interval: 30 seconds.
"""

import ccxt
import csv
import os
import time
from datetime import datetime, timezone

# ─── CONFIG ────────────────────────────────────────────────────────────────────

DEMO_HOST    = "https://cdn-ind.testnet.deltaex.org"
POLL_SECONDS = 30
LOG_FILE     = "trades_log.csv"

SYMBOLS = [
    "BTC/USD:USD",
    "ETH/USD:USD",
]

CONTRACT_SIZE = {
    "BTC/USD:USD": 0.001,
    "ETH/USD:USD": 0.01,
}

CSV_FIELDS = [
    "date", "symbol", "side", "lots", "contract_size",
    "entry_price", "exit_price", "sl_price", "tp_price",
    "pnl_usd", "result", "hold_time_min", "opened_at", "closed_at",
]

# ─── EXCHANGE ──────────────────────────────────────────────────────────────────

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
    if not key or not sec:
        raise SystemExit(".env missing API_KEY or API_SCECRET")

    ex = ccxt.delta({"apiKey": key or "", "secret": sec or "", "enableRateLimit": True})
    ex.urls = ex.urls or {}
    ex.urls["api"] = {"public": DEMO_HOST, "private": DEMO_HOST}
    return ex

# ─── CSV ───────────────────────────────────────────────────────────────────────

def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
        print(f"Created {LOG_FILE}")


def append_csv(row: dict):
    with open(LOG_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)
    print(f"  Saved → {LOG_FILE}")

# ─── SNAPSHOT ──────────────────────────────────────────────────────────────────

def snapshot_positions(ex):
    """Return {symbol: position_dict} for all open positions."""
    result = {}
    try:
        positions = ex.fetch_positions(SYMBOLS)
        for p in positions:
            if p.get("contracts") and float(p["contracts"]) != 0:
                result[p["symbol"]] = p
    except Exception as e:
        print(f"  fetch_positions error: {e}")
    return result


def snapshot_orders(ex, symbol):
    """Return open bracket orders for a symbol (reduceOnly)."""
    try:
        return ex.fetch_open_orders(symbol)
    except Exception:
        return []

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────

def main():
    print("="*55)
    print("  MONITOR: Delta India Demo Positions")
    print(f"  Log: {LOG_FILE}  |  Poll: {POLL_SECONDS}s")
    print("="*55)

    ex = make_exchange()
    ex.load_markets()
    init_csv()

    # ── Snapshot initial positions ──────────────────────────────
    prev = snapshot_positions(ex)
    if not prev:
        print("\nNo open positions found. Nothing to monitor.")
        return

    # Track entry metadata: {symbol: {entry_price, sl, tp, lots, side, opened_at}}
    meta = {}
    for sym, p in prev.items():
        orders = snapshot_orders(ex, sym)
        # Bracket orders: lower price = SL, higher = TP (for longs; reverse for shorts)
        prices = sorted([o.get("triggerPrice") or o.get("price", 0) for o in orders if o.get("price") or o.get("triggerPrice")])
        sl = prices[0]  if len(prices) >= 1 else None
        tp = prices[-1] if len(prices) >= 2 else None

        meta[sym] = {
            "entry_price": float(p.get("entryPrice") or 0),
            "sl":          sl,
            "tp":          tp,
            "lots":        int(float(p.get("contracts", 1))),
            "side":        p.get("side", "long"),
            "opened_at":   datetime.now(timezone.utc),
        }
        print(f"\n  Tracking: {sym}")
        print(f"    side={meta[sym]['side']}  lots={meta[sym]['lots']}  "
              f"entry={meta[sym]['entry_price']}  SL={sl}  TP={tp}")

    print(f"\nPolling every {POLL_SECONDS}s ... (Ctrl+C to stop)\n")

    # ── Poll loop ──────────────────────────────────────────────
    while True:
        time.sleep(POLL_SECONDS)
        now = datetime.now(timezone.utc)
        curr = snapshot_positions(ex)

        for sym in list(prev.keys()):
            if sym in curr:
                # Still open — show live PnL
                p = curr[sym]
                ticker = {}
                try:
                    ticker = ex.fetch_ticker(sym)
                except Exception:
                    pass
                px   = ticker.get("last", meta[sym]["entry_price"])
                cs   = CONTRACT_SIZE.get(sym, 0.001)
                lots = meta[sym]["lots"]
                epx  = meta[sym]["entry_price"]
                sign = 1 if meta[sym]["side"] == "long" else -1
                upnl = sign * (px - epx) * lots * cs
                print(f"[{now.strftime('%H:%M:%S')}] {sym} | price={px} | uPnL={upnl:+.4f} USD | waiting...")
            else:
                # Position CLOSED
                m       = meta[sym]
                cs      = CONTRACT_SIZE.get(sym, 0.001)
                lots    = m["lots"]
                epx     = m["entry_price"]
                sign    = 1 if m["side"] == "long" else -1

                # Determine exit via last trade/ticker
                try:
                    ticker  = ex.fetch_ticker(sym)
                    px = ticker.get("last", epx)
                    if m.get("side") == "long":
                        if m.get("sl") and px <= m["sl"]:
                            exit_px = m["sl"]
                        elif m.get("tp") and px >= m["tp"]:
                            exit_px = m["tp"]
                        else:
                            exit_px = px
                    else:
                        if m.get("sl") and px >= m["sl"]:
                            exit_px = m["sl"]
                        elif m.get("tp") and px <= m["tp"]:
                            exit_px = m["tp"]
                        else:
                            exit_px = px
                except Exception:
                    exit_px = epx

                pnl    = sign * (exit_px - epx) * lots * cs
                result = "win" if pnl >= 0 else "loss"
                hold   = round((now - m["opened_at"]).total_seconds() / 60, 1)

                row = {
                    "date":           now.strftime("%Y-%m-%d"),
                    "symbol":         sym,
                    "side":           m["side"],
                    "lots":           lots,
                    "contract_size":  cs,
                    "entry_price":    m["entry_price"],
                    "exit_price":     round(exit_px, 4),
                    "sl_price":       m["sl"],
                    "tp_price":       m["tp"],
                    "pnl_usd":        round(pnl, 4),
                    "result":         result,
                    "hold_time_min":  hold,
                    "opened_at":      m["opened_at"].strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "closed_at":      now.strftime("%Y-%m-%d %H:%M:%S UTC"),
                }

                print(f"\n{'='*55}")
                print(f"  CLOSED: {sym}  | {result.upper()}  | PnL={pnl:+.4f} USD")
                print(f"  entry={epx}  exit={exit_px}  hold={hold}min")
                print(f"{'='*55}\n")
                append_csv(row)
                del prev[sym]
                if sym in meta:
                    del meta[sym]

        if not prev:
            print("All positions closed. Done.")
            break

        prev = curr


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
