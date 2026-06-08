"""
state_manager.py
================
Persist and restore live_trade.py runtime state across restarts.

Files (always written next to this script, not CWD):
  open_positions.json  — tracker.open  (one entry per open trade)
  daily_state.json     — daily_trades + daily_loss (keyed by ISO date)
  seen_grabs.json      — grab keys already processed, with timestamps
"""

import json
import logging
import os
from datetime import datetime, timezone, date

log = logging.getLogger(__name__)

# ── Absolute paths (immune to working-directory changes) ──────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
POSITIONS_FILE  = os.path.join(BASE_DIR, "open_positions.json")
DAILY_FILE      = os.path.join(BASE_DIR, "daily_state.json")
SEEN_GRABS_FILE = os.path.join(BASE_DIR, "seen_grabs.json")

_GRAB_TTL = 86400   # 24 h in seconds


# ─── Serialisation helpers ────────────────────────────────────────────────────

def _serialize_meta(m: dict) -> dict:
    out = dict(m)
    if isinstance(out.get("opened_at"), datetime):
        out["opened_at"] = out["opened_at"].isoformat()
    return out


def _deserialize_meta(m: dict) -> dict:
    out = dict(m)
    if isinstance(out.get("opened_at"), str):
        try:
            dt = datetime.fromisoformat(out["opened_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out["opened_at"] = dt
        except ValueError:
            out["opened_at"] = datetime.now(timezone.utc)
    return out


# ─── Save ─────────────────────────────────────────────────────────────────────

def save_positions(open_dict: dict):
    """Write tracker.open to disk. Called after every add/remove."""
    try:
        payload = {k: _serialize_meta(v) for k, v in open_dict.items()}
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log.warning(f"[STATE] save_positions failed: {e}")


def save_daily(daily_trades: dict, daily_loss: dict):
    """Write today's trade count + loss to disk."""
    try:
        payload = {
            "trades": {str(k): v for k, v in daily_trades.items()},
            "loss":   {str(k): v for k, v in daily_loss.items()},
        }
        with open(DAILY_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log.warning(f"[STATE] save_daily failed: {e}")


def save_seen_grabs(seen: dict):
    """Persist seen_grabs dict, pruning entries older than 24 h."""
    try:
        now = datetime.now(timezone.utc).timestamp()
        pruned = {k: v for k, v in seen.items() if (now - v) < _GRAB_TTL}
        with open(SEEN_GRABS_FILE, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
        dropped = len(seen) - len(pruned)
        if dropped:
            log.info(f"[STATE] Pruned {dropped} stale grab keys (>24 h)")
    except Exception as e:
        log.warning(f"[STATE] save_seen_grabs failed: {e}")


# ─── Load ─────────────────────────────────────────────────────────────────────

def load_positions() -> dict:
    """Return restored tracker.open dict, or {} if no file / parse error."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        restored = {k: _deserialize_meta(v) for k, v in raw.items()}
        log.info(f"[STATE] Loaded {len(restored)} open position(s) from disk.")
        return restored
    except Exception as e:
        log.warning(f"[STATE] load_positions failed: {e}")
        return {}


def load_daily() -> tuple[dict, dict]:
    """Return (daily_trades, daily_loss) for today only, or empty dicts."""
    if not os.path.exists(DAILY_FILE):
        return {}, {}
    try:
        with open(DAILY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        today  = date.today()
        trades = {}
        loss   = {}
        for k, v in raw.get("trades", {}).items():
            try:
                d = date.fromisoformat(k)
                if d == today:
                    trades[d] = int(v)
            except ValueError:
                pass
        for k, v in raw.get("loss", {}).items():
            try:
                d = date.fromisoformat(k)
                if d == today:
                    loss[d] = float(v)
            except ValueError:
                pass
        log.info(f"[STATE] Daily state restored: trades={trades}, loss={loss}")
        return trades, loss
    except Exception as e:
        log.warning(f"[STATE] load_daily failed: {e}")
        return {}, {}


def load_seen_grabs() -> dict:
    """Restore seen_grabs dict, pruning stale entries. Handles legacy list format."""
    if not os.path.exists(SEEN_GRABS_FILE):
        return {}
    try:
        with open(SEEN_GRABS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Back-compat: old format was a plain list
        if isinstance(data, list):
            now_ts = datetime.now(timezone.utc).timestamp()
            data = {k: now_ts for k in data}
        now     = datetime.now(timezone.utc).timestamp()
        restored = {k: v for k, v in data.items() if (now - v) < _GRAB_TTL}
        dropped  = len(data) - len(restored)
        log.info(f"[STATE] Loaded {len(restored)} seen grab keys (pruned {dropped} stale).")
        return restored
    except Exception as e:
        log.warning(f"[STATE] load_seen_grabs failed: {e}")
        return {}


# ─── Reconcile ────────────────────────────────────────────────────────────────

def reconcile_positions(restored: dict, ex, append_csv_fn, tracker_logged: set) -> dict:
    """
    Cross-check restored positions against the exchange.

    - Still open  → keep in tracker.open
    - Closed      → log to CSV, drop from tracker
    - Fetch error → assume still open (safe default, avoids wiping on network blip)

    Returns the cleaned dict (only confirmed-open + assumed-open positions).
    """
    if not restored:
        return {}

    log.info("[STATE] Reconciling restored positions with exchange...")
    keep = {}

    for sym_key, m in restored.items():
        ccxt_sym = m.get("ccxt_sym", "")
        try:
            positions  = ex.fetch_positions([ccxt_sym])
            still_open = any(
                p.get("contracts") and float(p["contracts"]) != 0
                for p in positions
            )
        except Exception as e:
            log.warning(f"[STATE] fetch_positions failed for {sym_key}: {e}. Assuming open.")
            keep[sym_key] = m   # safe default
            continue

        if still_open:
            log.info(f"[STATE]  {sym_key}: still open — restored.")
            keep[sym_key] = m
        else:
            log.info(f"[STATE]  {sym_key}: closed while offline — logging.")
            _log_closed_while_down(sym_key, m, ex, append_csv_fn, tracker_logged)

    return keep


def _log_closed_while_down(sym_key, m, ex, append_csv_fn, tracker_logged):
    """Best-effort log of a trade that closed while the bot was offline."""
    trade_key = f"{sym_key}_{m['opened_at'].isoformat()}"
    if trade_key in tracker_logged:
        return

    now        = datetime.now(timezone.utc)
    ccxt_sym   = m.get("ccxt_sym", "")
    opened_ts  = int(m["opened_at"].timestamp() * 1000)
    close_side = "sell" if m["side"] == "buy" else "buy"
    exit_px    = None

    # Try actual fill from closed orders
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
            exit_px = float(o.get("average") or o["price"])
    except Exception as e:
        log.debug(f"[STATE] fetch_closed_orders for {sym_key}: {e}")

    # Fallback: snap to SL or TP
    if exit_px is None:
        try:
            ticker_px = float(ex.fetch_ticker(ccxt_sym)["last"])
            if m["side"] == "buy":
                exit_px = m["tp"] if ticker_px >= m["tp"] else m["sl"]
            else:
                exit_px = m["tp"] if ticker_px <= m["tp"] else m["sl"]
        except Exception:
            exit_px = m["entry"]

    sign = 1 if m["side"] == "buy" else -1
    pnl  = sign * (exit_px - m["entry"]) * m["lots"] * m["contract_size"]
    hold = round((now - m["opened_at"]).total_seconds() / 60, 1)

    row = {
        "date":          now.strftime("%Y-%m-%d"),
        "symbol":        ccxt_sym,
        "side":          m["side"],
        "lots":          m["lots"],
        "contract_size": m["contract_size"],
        "entry_price":   round(m["entry"], 4),
        "exit_price":    round(exit_px, 4),
        "sl_price":      round(m["sl"], 4),
        "tp_price":      round(m["tp"], 4),
        "pnl_usd":       round(pnl, 4),
        "result":        "win" if pnl >= 0 else "loss",
        "hold_time_min": hold,
        "opened_at":     m["opened_at"].strftime("%Y-%m-%d %H:%M:%S UTC"),
        "closed_at":     now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    try:
        append_csv_fn(row)
        tracker_logged.add(trade_key)
        emoji = "🟢" if pnl >= 0 else "🔴"
        log.info(f"[STATE] Logged offline-close {sym_key}: {'WIN' if pnl>=0 else 'LOSS'} PnL={pnl:+.4f}")
        try:
            from telegram_alerts import send as tg
            tg(
                f"{emoji} <b>CLOSED (while offline)</b>: {sym_key} {m['side'].upper()}\n"
                f"PnL: {pnl:+.4f} USD | Exit: {exit_px:.2f}\n"
                f"Hold: {hold}min | {'WIN' if pnl>=0 else 'LOSS'}"
            )
        except Exception:
            pass
    except Exception as e:
        log.warning(f"[STATE] append_csv for offline-close failed: {e}")