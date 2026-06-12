# bot_server.py
# ============================================================
# Telegram chatbot — runs as daemon thread inside live_trade.py
# Shares live state: tracker, daily_trades, daily_loss, kill_switch
# LLM powered by OpenRouter (freeform market Q&A)
#
# Commands:
#   /status            — open trades + uPnL
#   /daily             — today's trade count + loss
#   /price             — live BTC + ETH price
#   /pause             — pause signal scanning
#   /resume            — resume signal scanning
#   /long BTCUSDT      — manual long entry (asks confirmation)
#   /short ETHUSDT     — manual short entry (asks confirmation)
#   /close BTCUSDT     — close specific position (asks confirmation)
#   /closeall          — close ALL positions (asks confirmation)
#   /emergency         — instantly close ALL, no confirmation
#   /help              — list commands
#   (any text)         — routed to OpenRouter LLM with trading context
# ============================================================

import threading
import time
import logging
import requests
from datetime import datetime, timezone
from virtual_exchange import VirtualExchange
from options_bot_commands import handle_options_commands
from options_paper_engine import get_engine
from options_monitor import start_monitor
from fyers_data import get_fyers

vx = VirtualExchange()

log = logging.getLogger(__name__)

# ─── Load .env ───────────────────────────────────────────────────────────────

def _load_env():
    env = {}
    try:
        for line in open(".env"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

_ENV = _load_env()
BOT_TOKEN = _ENV.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = _ENV.get("TELEGRAM_CHAT_ID", "")
OPENROUTER_KEY = _ENV.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = _ENV.get("OPENROUTER_MODEL", "deepseek/deepseek-chat")

POLL_INTERVAL = 2   # seconds between getUpdates polls

# ─── Shared state (injected by live_trade.py) ────────────────────────────────

_tracker = None
_daily_trades = None
_daily_loss = None
_exchange = None
_kill_switch = None

# Pending confirmations: chat_id → {action, params, expires}
_pending: dict[str, dict] = {}
CONFIRM_TIMEOUT = 30   # seconds before confirmation expires

# LLM cancellation: tracks latest request token per chat_id
# If a new message arrives before LLM responds, old reply is discarded
_llm_tokens: dict[str, int] = {}

_conv_history: dict[str, list] = {}
MAX_HISTORY = 10

# Symbol map for manual trades
SYMBOL_MAP = {
    "BTCUSDT": {"ccxt": "BTC/USD:USD", "contract_size": 0.001},
    "ETHUSDT": {"ccxt": "ETH/USD:USD", "contract_size": 0.01},
}
DEFAULT_MANUAL_LOTS = 1   # lots for manual trades


# ─── Init ────────────────────────────────────────────────────────────────────

def init(tracker, daily_trades, daily_loss, exchange, kill_switch):
    global _tracker, _daily_trades, _daily_loss, _exchange, _kill_switch
    _tracker      = tracker
    _daily_trades = daily_trades
    _daily_loss   = daily_loss
    _exchange     = exchange
    _kill_switch  = kill_switch
    log.info("[BOT] Initialized with shared state.")


# ─── Telegram helpers ────────────────────────────────────────────────────────

def _send(chat_id: str, text: str):
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log.warning(f"[BOT] send failed: {e}")


def _send_photo(chat_id: str, photo_path: str, caption: str = ""):
    if not BOT_TOKEN:
        return
    try:
        with open(photo_path, 'rb') as photo:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": photo},
                timeout=15,
            )
    except Exception as e:
        log.warning(f"[BOT] sendPhoto failed: {e}")
        _send(chat_id, f"❌ Failed to send chart: {e}")


def _generate_market_chart(symbol: str, entry: float = None, sl: float = None, tp: float = None) -> str:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import pandas as pd
    import numpy as np
    
    ccxt_sym = SYMBOL_MAP.get(symbol, {}).get("ccxt") or symbol
    try:
        raw = _exchange.fetch_ohlcv(ccxt_sym, "15m", limit=40)
        df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    except Exception as e:
        log.warning(f"Failed to fetch candles for chart: {e}")
        return ""
        
    # Deduce trade parameters for label details
    side = "buy"
    if entry and tp:
        side = "buy" if tp > entry else "sell"
        
    lots = 1
    contract_size = 0.001 if "BTC" in symbol else 0.01
    if _tracker and symbol in _tracker.open:
        m = _tracker.open[symbol]
        lots = m.get("lots", 1)
        contract_size = m.get("contract_size", contract_size)
        side = m.get("side", side)
        
    N = len(df)
    x = np.arange(N)
    
    fig, ax = plt.subplots(figsize=(10, 6), facecolor='#131722')
    ax.set_facecolor('#131722')
    
    # 1. Plot Candlesticks
    for i in range(N):
        row = df.iloc[i]
        color = '#089981' if row['close'] >= row['open'] else '#f23645'
        # Wick
        ax.plot([i, i], [row['low'], row['high']], color=color, linewidth=1.5, zorder=2)
        # Body
        body_bottom = min(row['open'], row['close'])
        body_height = abs(row['close'] - row['open'])
        if body_height == 0:
            body_height = (df['high'].max() - df['low'].min()) * 0.003
        rect = plt.Rectangle((i - 0.3, body_bottom), 0.6, body_height, facecolor=color, edgecolor=color, zorder=3)
        ax.add_patch(rect)
        
    # 2. Add EMA9 indicator (Blue line)
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    ax.plot(x, df['ema9'], color='#2196f3', label='EMA 9', linewidth=1.5, zorder=4)
    
    # 3. Format grid and axis
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.tick_params(colors='#848e9c', labelsize=9)
    ax.grid(True, which='both', axis='y', color='#2a2e39', linestyle='-', linewidth=0.5, zorder=1)
    
    # Remove outer box lines
    for spine in ax.spines.values():
        spine.set_color('#2a2e39')
        
    # 4. Draw Entry, SL, TP lines & TradingView boxes
    if entry:
        ax.axhline(y=entry, color='#2962ff', linestyle='-', linewidth=1.5, zorder=5)
        text_str = f" {lots} |  0.00 USD  "
        ax.text(N + 0.3, entry, text_str, color='white', fontsize=8, fontweight='bold', va='center',
                bbox=dict(facecolor='#2962ff', edgecolor='none', boxstyle='round,pad=0.3', alpha=0.9))
                
    if tp:
        ax.axhline(y=tp, color='#089981', linestyle='-', linewidth=1.5, zorder=5)
        pnl = 0.0
        if entry:
            sign = 1 if side == "buy" else -1
            pnl = sign * (tp - entry) * lots * contract_size
        text_str = f" {lots} | +{pnl:.2f} USD "
        ax.text(N + 0.3, tp, text_str, color='white', fontsize=8, fontweight='bold', va='center',
                bbox=dict(facecolor='#089981', edgecolor='none', boxstyle='round,pad=0.3', alpha=0.9))
                
    if sl:
        ax.axhline(y=sl, color='#f23645', linestyle='-', linewidth=1.5, zorder=5)
        pnl = 0.0
        if entry:
            sign = 1 if side == "buy" else -1
            pnl = sign * (sl - entry) * lots * contract_size
        text_str = f" {lots} | {pnl:.2f} USD "
        ax.text(N + 0.3, sl, text_str, color='white', fontsize=8, fontweight='bold', va='center',
                bbox=dict(facecolor='#f23645', edgecolor='none', boxstyle='round,pad=0.3', alpha=0.9))
                
    # 5. Live Price indicator box
    last_px = df['close'].iloc[-1]
    curr_color = '#089981' if df['close'].iloc[-1] >= df['open'].iloc[-1] else '#f23645'
    ax.text(N + 0.3, last_px, f" {last_px:.2f} ", color='white', fontsize=8, fontweight='bold', va='center',
            bbox=dict(facecolor=curr_color, edgecolor='none', boxstyle='round,pad=0.3'))
            
    # Set X-Axis Ticks (displaying formatted time labels)
    step = max(1, N // 5)
    tick_indices = list(range(0, N, step))
    tick_labels = [df['timestamp'].iloc[idx].strftime('%d-%m %H:%M') for idx in tick_indices]
    ax.set_xticks(tick_indices)
    ax.set_xticklabels(tick_labels, rotation=0, color='#848e9c')
    
    # Chart margins and title
    ax.set_xlim(-1, N + 4)
    y_min = min(df['low'].min(), sl or entry or last_px) * 0.998
    y_max = max(df['high'].max(), tp or entry or last_px) * 1.002
    ax.set_ylim(y_min, y_max)
    
    plt.title(f"MARK:{symbol} · 15 · Delta", fontsize=11, fontweight='bold', color='#d1d4dc', loc='left', pad=10)
    plt.tight_layout()
    
    path = "market_chart.png"
    plt.savefig(path, facecolor='#131722', edgecolor='none', bbox_inches='tight')
    plt.close()
    return path


def _update_bracket_orders(chat_id: str, sym_key: str, new_sl: float = None, new_tp: float = None):
    with _tracker._lock:
        if not _tracker or sym_key.upper() not in _tracker.open:
            _send(chat_id, f"❌ No open position found in tracker for {sym_key}.")
            return False
            
        m = _tracker.open[sym_key.upper()]
    
    ccxt_sym = m["ccxt_sym"]
    lots = m["lots"]
    side = m["side"]
    close_side = "sell" if side == "buy" else "buy"
    
    if _exchange is None:
        _send(chat_id, "❌ Exchange not initialized.")
        return False
        
    try:
        try:
            open_orders = _exchange.fetch_open_orders(ccxt_sym)
            for o in open_orders:
                if o.get("reduceOnly") or o.get("info", {}).get("reduce_only") or o.get("type") == "stop":
                    _exchange.cancel_order(o["id"], ccxt_sym)
        except Exception as e:
            log.warning(f"Failed to cancel old bracket orders: {e}")
            
        if new_sl:
            m["sl"] = new_sl
        if new_tp:
            m["tp"] = new_tp

        # Persist updated SL/TP to disk
        with _tracker._lock:
            from state_manager import save_positions
            save_positions(_tracker.open)
            
        sl_placed = False
        if m["sl"]:
            try:
                _exchange.create_order(
                    ccxt_sym, "stop", close_side, lots,
                    params={
                        "stopPrice": str(round(m["sl"], 2)),
                        "reduce_only": True
                    }
                )
                sl_placed = True
            except Exception as e:
                log.error(f"Failed to place new Stop Loss order: {e}")
                _send(chat_id, f"⚠️ Failed to place SL on exchange: {e}")
                
        tp_placed = False
        if m["tp"]:
            try:
                _exchange.create_order(
                    ccxt_sym, "limit", close_side, lots, m["tp"],
                    params={"reduce_only": True}
                )
                tp_placed = True
            except Exception as e:
                log.error(f"Failed to place new Take Profit order: {e}")
                _send(chat_id, f"⚠️ Failed to place TP on exchange: {e}")
                
        _send(chat_id, 
            f"✅ <b>Brackets Updated for {sym_key}</b>\n"
            f"New SL: {m['sl']:.2f} {'(Exchange OK)' if sl_placed else '(Local Only)'}\n"
            f"New TP: {m['tp']:.2f} {'(Exchange OK)' if tp_placed else '(Local Only)'}"
        )
        return True
    except Exception as e:
        _send(chat_id, f"❌ Error updating brackets: {e}")
        return False


def _parse_sl(text: str, side: str, entry_px: float) -> float:
    text = text.strip().lower()
    if text == "auto":
        return round(entry_px * 0.995, 2) if side == "buy" else round(entry_px * 1.005, 2)
    if text.endswith("%"):
        try:
            pct = float(text[:-1]) / 100.0
            return round(entry_px * (1.0 - pct), 2) if side == "buy" else round(entry_px * (1.0 + pct), 2)
        except ValueError:
            return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _parse_tp(text: str, side: str, entry_px: float, sl_px: float) -> float:
    text = text.strip().lower()
    risk = abs(entry_px - sl_px)
    if text == "auto":
        return round(entry_px + risk * 3.0, 2) if side == "buy" else round(entry_px - risk * 3.0, 2)
    if ":" in text or text.startswith("rr"):
        try:
            val = text.replace("rr", "").replace(" ", "")
            parts = val.split(":")
            ratio = float(parts[1]) if len(parts) > 1 else float(parts[0])
            return round(entry_px + risk * ratio, 2) if side == "buy" else round(entry_px - risk * ratio, 2)
        except Exception:
            return None
    if text.endswith("%"):
        try:
            pct = float(text[:-1]) / 100.0
            return round(entry_px * (1.0 + pct), 2) if side == "buy" else round(entry_px * (1.0 - pct), 2)
        except ValueError:
            return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _get_updates(offset: int):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"timeout": 20, "offset": offset},
            timeout=25,
        )
        return r.json().get("result", [])
    except Exception as e:
        log.warning(f"[BOT] getUpdates failed: {e}")
        return []


# ─── Stale position purge ────────────────────────────────────────────────────

def _purge_stale_positions():
    """
    Cross-check tracker.open against exchange.
    Remove any entry where exchange shows no open position.
    Called before any command that reads tracker.open.
    """
    if not _tracker or not _exchange:
        return
    with _tracker._lock:
        sym_keys = list(_tracker.open.keys())
    for sym_key in sym_keys:
        with _tracker._lock:
            m = _tracker.open.get(sym_key)
        if not m:
            continue
        try:
            pos = _exchange.fetch_positions([m["ccxt_sym"]])
            still_open = any(
                p.get("contracts") and float(p["contracts"]) != 0
                for p in pos
            )
            # Only purge if exchange explicitly returned position data showing it's closed.
            # Empty list means API may not support this query — keep the position to avoid false purges.
            if not still_open and pos:
                _tracker.remove(sym_key)
                log.info(f"[BOT] Purged stale tracker entry: {sym_key}")
        except Exception as e:
            log.warning(f"[BOT] Could not verify position for {sym_key}: {e} — keeping in tracker.")


# ─── Live data helpers ───────────────────────────────────────────────────────

def _fetch_price(symbol: str):
    if _exchange is None:
        return None
    try:
        return float(_exchange.fetch_ticker(symbol)["last"])
    except Exception:
        return None


def _open_trades_summary() -> str:
    if not _tracker or not _tracker.open:
        return "No open trades."
    lines = []
    with _tracker._lock:
        trades_items = list(_tracker.open.items())
    for sym_key, m in trades_items:
        px   = _fetch_price(m["ccxt_sym"])
        upnl = ""
        if px:
            sign = 1 if m["side"] == "buy" else -1
            usd  = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
            upnl = f"  uPnL: <b>{usd:+.4f} USD</b>"
        age = round((datetime.now(timezone.utc) - m["opened_at"]).total_seconds() / 60, 1)
        
        trail_status = f"Trail: {m['trail_dist']:.2f} USD" if m.get("trail_dist") else "Trail: OFF"
        
        lines.append(
            f"• <b>{sym_key}</b> {m['side'].upper()} x{m['lots']} ({trail_status})\n"
            f"  Entry: {m['entry']:.2f} | SL: {m['sl']:.2f} | TP: {m['tp']:.2f}\n"
            f"  Live px: {px or '?'} | Age: {age}min{upnl}\n"
            f"  🎛 <b>Controls:</b> /chart_{sym_key} | /trail_{sym_key} | /modify_{sym_key}"
        )
    return "\n\n".join(lines)


def _closed_trades_summary(limit: int = 5) -> str:
    import csv
    import os
    if not os.path.exists("trades_log.csv"):
        return "\n\n📜 <b>Recent Closed Trades</b>\nNo trade history logged."
    
    try:
        rows = []
        with open("trades_log.csv", "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        
        if not rows:
            return "\n\n📜 <b>Recent Closed Trades</b>\nNo trades logged yet."
        
        # Take the last `limit` rows
        recent = rows[-limit:]
        lines = []
        
        for r in recent:
            pnl_val = r.get("pnl_usd", "0.0")
            try:
                pnl = float(pnl_val)
            except ValueError:
                pnl = 0.0
            res = r.get("result", "").upper()
            sym = r.get("symbol", "").split(":")[0]
            side = r.get("side", "").upper()
            
            try:
                entry = float(r.get("entry_price", 0.0))
                exit_px = float(r.get("exit_price", 0.0))
                entry_str = f"{entry:.2f}"
                exit_str = f"{exit_px:.2f}"
            except ValueError:
                entry_str = r.get("entry_price", "N/A")
                exit_str = r.get("exit_price", "N/A")
                
            emoji = "🟢" if pnl >= 0 else "🔴"
            
            lines.append(
                f"{emoji} <b>{sym}</b> {side} | {res} | <b>{pnl:+.2f} USD</b>\n"
                f"  Entry: {entry_str} ➔ Exit: {exit_str} ({r.get('date', '')})"
            )
            
        summary_text = "\n".join(lines)
        return f"\n\n📜 <b>Recent Closed Trades (Last {len(recent)})</b>\n\n{summary_text}"
    except Exception as e:
        log.warning(f"Error reading trades log: {e}")
        return f"\n\n📜 Error reading trade history: {e}"


def _daily_summary() -> str:
    import csv
    import os
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    trades_today = 0
    total_profit = 0.0
    total_loss = 0.0
    net_pnl = 0.0
    trade_details = []
    
    if os.path.exists("trades_log.csv"):
        try:
            with open("trades_log.csv", "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("date") == today_str:
                        trades_today += 1
                        pnl_val = row.get("pnl_usd", "0.0")
                        try:
                            pnl = float(pnl_val)
                        except ValueError:
                            pnl = 0.0
                        
                        net_pnl += pnl
                        if pnl >= 0:
                            total_profit += pnl
                        else:
                            total_loss += abs(pnl)
                        
                        sym = row.get("symbol", "").split(":")[0]
                        side = row.get("side", "").upper()
                        res = row.get("result", "").upper()
                        emoji = "🟢" if pnl >= 0 else "🔴"
                        trade_details.append(f"  {emoji} {sym} {side} ({res}): <b>{pnl:+.2f} USD</b>")
        except Exception as e:
            log.warning(f"Error parsing trades_log.csv for daily summary: {e}")
            
    paused = "⏸ PAUSED" if (_kill_switch and _kill_switch.is_set()) else "▶ RUNNING"
    
    lines = [
        f"📅 <b>Today ({today_str})</b>",
        f"Trades: {trades_today}",
        f"Profit: {total_profit:.2f} USD",
        f"Loss: {total_loss:.2f} USD",
        f"Net PnL: <b>{net_pnl:+.2f} USD</b>",
        f"Bot status: {paused}"
    ]
    
    if trade_details:
        lines.append("\n📝 <b>Today's Trades:</b>")
        lines.extend(trade_details)
        
    return "\n".join(lines)


def _prices_summary() -> str:
    btc = _fetch_price("BTC/USD:USD")
    eth = _fetch_price("ETH/USD:USD")
    return (
        f"💹 <b>Live Prices</b>\n"
        f"BTC: {'${:,.2f}'.format(btc) if btc else 'N/A'}\n"
        f"ETH: {'${:,.2f}'.format(eth) if eth else 'N/A'}"
    )


# ─── Manual trade execution ──────────────────────────────────────────────────

def _exec_manual_trade(chat_id: str, sym_key: str, side: str, lots: int = DEFAULT_MANUAL_LOTS, leverage: int = None, sl: float = None, tp: float = None):
    """Place a manual market order with custom or auto SL/TP."""
    info = SYMBOL_MAP.get(sym_key.upper())
    if not info:
        _send(chat_id, f"❌ Unknown symbol: {sym_key}. Use BTCUSDT or ETHUSDT.")
        return

    ccxt_sym = info["ccxt"]
    contract_size = info["contract_size"]

    if leverage is not None and _exchange is not None:
        try:
            _exchange.set_leverage(int(leverage), ccxt_sym)
            _send(chat_id, f"⚙️ Leverage set to {leverage}x for {sym_key} on exchange.")
        except Exception as e:
            log.warning(f"[BOT] Failed to set leverage: {e}")
            _send(chat_id, f"⚠️ Could not set leverage on exchange: {e}")

    px = _fetch_price(ccxt_sym)
    if not px:
        _send(chat_id, "❌ Could not fetch price. Try again.")
        return

    # Use custom sl/tp if provided, otherwise compute default
    if sl is None:
        if side == "buy":
            sl = round(px * 0.995, 2)
        else:
            sl = round(px * 1.005, 2)
    if tp is None:
        if side == "buy":
            tp = round(px * 1.010, 2)
        else:
            tp = round(px * 0.990, 2)

    if _exchange is None:
        _send(chat_id, "❌ Exchange not initialized.")
        return

    try:
        order = _exchange.create_order(
            ccxt_sym, "market", side, lots,
            params={
                "bracket_stop_loss_price":         str(sl),
                "bracket_stop_loss_limit_price":   str(sl),
                "bracket_take_profit_price":       str(tp),
                "bracket_take_profit_limit_price": str(tp),
            },
        )
        actual_entry = float(order.get("average") or order.get("price") or px)

        # Register in tracker so bot monitors it — add() auto-persists via save_positions
        if _tracker is not None:
            _tracker.add(sym_key.upper(), {
                "ccxt_sym":      ccxt_sym,
                "side":          side,
                "lots":          lots,
                "entry":         actual_entry,
                "sl":            sl,
                "tp":            tp,
                "opened_at":     datetime.now(timezone.utc),
                "contract_size": contract_size,
            })
            # Also increment daily_trades so limit logic stays accurate
            if _daily_trades is not None:
                day = datetime.now(timezone.utc).date()
                _daily_trades[day] = _daily_trades.get(day, 0) + 1
                from state_manager import save_daily
                if _daily_loss is not None:
                    save_daily(_daily_trades, _daily_loss)

        direction = "LONG" if side == "buy" else "SHORT"
        _send(chat_id,
            f"✅ <b>MANUAL {direction} PLACED</b>\n"
            f"Symbol: {sym_key}\n"
            f"Entry: {actual_entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}\n"
            f"Lots: {lots} | Order ID: {order.get('id', '?')}"
        )
        log.info(f"[BOT] Manual {direction} {sym_key} placed. entry={actual_entry}")

    except Exception as e:
        _send(chat_id, f"❌ Order failed: {e}")
        log.error(f"[BOT] Manual trade error: {e}")


def _exec_close(chat_id: str, sym_key: str):
    """Close a specific open position."""
    if not _tracker or sym_key.upper() not in _tracker.open:
        _send(chat_id, f"❌ No open position for {sym_key}.")
        return

    m = _tracker.open[sym_key.upper()]
    ccxt_sym = m["ccxt_sym"]
    lots = m["lots"]
    close_side = "sell" if m["side"] == "buy" else "buy"

    if _exchange is None:
        _send(chat_id, "❌ Exchange not initialized.")
        return

    try:
        # Cancel bracket orders first
        try:
            open_orders = _exchange.fetch_open_orders(ccxt_sym)
            for o in open_orders:
                _exchange.cancel_order(o["id"], ccxt_sym)
        except Exception:
            pass

        order = _exchange.create_order(ccxt_sym, "market", close_side, lots,
                                        params={"reduce_only": True})
        px = float(order.get("average") or order.get("price") or 0)

        # Compute PnL + update daily_loss
        sign = 1 if m["side"] == "buy" else -1
        pnl  = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
        if pnl < 0 and _daily_loss is not None:
            day = datetime.now(timezone.utc).date()
            _daily_loss[day] = _daily_loss.get(day, 0) + abs(pnl)
            try:
                from state_manager import save_daily
                if _daily_trades is not None:
                    save_daily(_daily_trades, _daily_loss)
            except Exception as e:
                log.warning(f"[BOT] save_daily after manual close failed: {e}")

        _send(chat_id,
            f"🔒 <b>CLOSED {sym_key}</b>\n"
            f"Exit: {px:.2f} | PnL: {pnl:+.4f} USD\n"
            f"Order ID: {order.get('id', '?')}"
        )

        _tracker.remove(sym_key.upper())
        log.info(f"[BOT] Manual close {sym_key} at {px} | PnL={pnl:+.4f}")

    except Exception as e:
        _send(chat_id, f"❌ Close failed: {e}")
        log.error(f"[BOT] Close error: {e}")


def _exec_closeall(chat_id: str):
    """Close all open positions."""
    if not _tracker or not _tracker.open:
        _send(chat_id, "No open positions to close.")
        return

    syms = list(_tracker.open.keys())
    _send(chat_id, f"🔒 Closing {len(syms)} position(s)...")
    for sym_key in syms:
        _exec_close(chat_id, sym_key)


# ─── Confirmation system ─────────────────────────────────────────────────────

def _ask_confirm(chat_id: str, action: str, params: dict, preview: str):
    """Store pending action and ask user to confirm."""
    _pending[chat_id] = {
        "action":  action,
        "params":  params,
        "expires": time.time() + CONFIRM_TIMEOUT,
    }
    _send(chat_id,
        f"⚠️ <b>Confirm?</b>\n{preview}\n\n"
        f"Reply <b>YES</b> to execute or <b>NO</b> to cancel.\n"
        f"(expires in {CONFIRM_TIMEOUT}s)"
    )


def _handle_confirm(chat_id: str, text: str) -> bool:
    """Check if message is a YES/NO or interactive response. Returns True if handled."""
    pending = _pending.get(chat_id)
    if not pending:
        return False

    if time.time() > pending["expires"]:
        del _pending[chat_id]
        _send(chat_id, "⏰ Session expired. Please restart the command.")
        return True

    action = pending["action"]
    p = pending["params"]

    if action == "ask_sl":
        sl = _parse_sl(text, p["side"], p["px"])
        if sl is None:
            _send(chat_id, "❌ Invalid Stop Loss format. Enter a price (e.g. <code>68500</code>), percent (e.g. <code>0.5%</code>), or <code>auto</code>:")
            return True
        p["sl"] = sl
        pending["action"] = "ask_tp"
        pending["expires"] = time.time() + CONFIRM_TIMEOUT * 2
        
        _send(chat_id,
            f"🎯 <b>Stop Loss set to: {sl:.2f}</b>\n\n"
            f"Now, please specify your Take Profit (TP). You can enter:\n"
            f"- Exact price (e.g. <code>72000</code>)\n"
            f"- Risk-to-Reward ratio (e.g. <code>1:2</code> or <code>1:3</code>)\n"
            f"- Percentage (e.g. <code>1.5%</code>)\n"
            f"- <code>auto</code> for default 1:3 RR"
        )
        return True

    elif action == "ask_tp":
        tp = _parse_tp(text, p["side"], p["px"], p["sl"])
        if tp is None:
            _send(chat_id, "❌ Invalid Take Profit format. Enter a price, Risk:Reward (e.g. <code>1:2</code>), percent, or <code>auto</code>:")
            return True
        p["tp"] = tp
        
        lots_desc = f"{p['lots']} (Manual)" if p.get('lots_desc_type') == 'manual' else f"{p['lots']} (Auto)"
        leverage_desc = f"{p['leverage']}x" if p.get('leverage') else "Default"
        direction_emoji = "📈 LONG" if p["side"] == "buy" else "📉 SHORT"
        
        _ask_confirm(chat_id, "execute_trade", p,
            f"{direction_emoji} <b>{p['sym']} Final Review</b>\n"
            f"Entry Price: {p['px']:.2f}\n"
            f"Lots: {lots_desc}\n"
            f"Leverage: {leverage_desc}\n"
            f"Stop Loss: <b>{p['sl']:.2f}</b>\n"
            f"Take Profit: <b>{p['tp']:.2f}</b>"
        )
        return True

    word = text.strip().upper()

    if word == "YES":
        del _pending[chat_id]
        
        if action == "execute_trade":
            _exec_manual_trade(chat_id, p["sym"], p["side"], p["lots"], p.get("leverage"), p["sl"], p["tp"])
        elif action == "close":
            _exec_close(chat_id, p["sym"])
        elif action == "closeall":
            _exec_closeall(chat_id)
        return True

    elif word == "NO":
        del _pending[chat_id]
        _send(chat_id, "❌ Cancelled.")
        return True

    return False


# ─── OpenRouter LLM ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an AI assistant embedded inside a live trading bot that handles multiple markets:

1. CRYPTO: BTC/USD and ETH/USD perpetuals on Delta Exchange India (demo/paper account).
   Strategy: Smart Money Concepts (SMC) — 4H liquidity grabs + 15M BOS + FVG entries.

2. INDIAN OPTIONS (Paper Trading): NIFTY, BANKNIFTY, SENSEX options via Fyers API.
   Strategy: 5m PDH/PDL breakout + PCR + OI walls + VIX bias.
   Engine: OptionsPaperEngine — tracks open CE/PE positions with SL/TP.

3. INDIAN STOCKS (Virtual Exchange): Manual paper trades via VirtualExchange.

You have full awareness of all three accounts. When the user mentions NIFTY, BANKNIFTY, options, CE, PE, PDH, PDL, PCR, OI — respond in that context.
When they mention BTC, ETH, Delta, crypto — respond in that context.
Be concise and direct. Use numbers. Never say something is "not in scope" — you cover all three markets.

IMPORTANT — FUNCTION CALLS:
When the user asks you to perform an action (set SL, set TP, close position, buy option, sell option),
you MUST respond with a JSON function call block in this exact format (and nothing else):

FUNCTION_CALL: {"action": "<action>", "params": {<params>}}

Supported actions and their params:
- set_tp:      {"key": "NIFTY_24500CE_26JUN", "tp": 107.0}
- set_sl:      {"key": "NIFTY_24500CE_26JUN", "sl": 32.0}
- close_opt:   {"key": "NIFTY_24500CE_26JUN"}
- buy_opt:     {"underlying": "NIFTY", "strike": 24500, "opt_type": "CE", "expiry": "26JUN", "lots": 1}
- sell_opt:    {"underlying": "NIFTY", "strike": 24500, "opt_type": "CE", "expiry": "26JUN", "lots": 1}
- opt_status:  {}
- opt_pnl:     {}

If you cannot determine a required param (e.g. which position key), ask the user to clarify.
Only use FUNCTION_CALL when the user is clearly asking to execute something.
For analysis, advice, or questions — reply normally in plain text."""

_SYSTEM_PROMPT_OPTIONS = """You are an expert Indian options trader and analyst assistant embedded in a live trading bot.
You specialize in NIFTY, BANKNIFTY, and SENSEX options paper trading.

Your expertise:
- PDH/PDL breakout strategy on 5m charts
- PCR (Put-Call Ratio) analysis — bullish >1.3, bearish <0.75
- OI (Open Interest) analysis — CE wall = resistance, PE wall = support
- Max pain theory
- VIX interpretation — high VIX >18 prefer selling, low VIX <12 prefer buying
- Greeks basics (Delta, Theta, Vega)
- Multi-leg strategies: straddle, strangle, iron condor, bull call spread, bear put spread

Always reference the live positions, capital, and PnL from the injected context.
Be direct, use numbers, give specific strike/premium/lot recommendations.
When user asks to execute something, use FUNCTION_CALL format.

FUNCTION_CALL format for actions:
FUNCTION_CALL: {"action": "<action>", "params": {<params>}}
Actions: set_tp, set_sl, close_opt, buy_opt, sell_opt, opt_status, opt_pnl"""

_SYSTEM_PROMPT_CRYPTO = """You are an expert crypto futures trader assistant embedded in a live trading bot.
You specialize in BTC/USD and ETH/USD perpetuals on Delta Exchange India (demo account).

Your strategy: Smart Money Concepts (SMC)
- 4H timeframe: Liquidity grabs (equal highs/lows, stop hunts)
- 15M timeframe: Break of Structure (BOS) confirmation + Fair Value Gap (FVG) entries
- Risk: 1% per trade, 1:3 RR minimum
- Session focus: London + NY overlap

Always reference the live BTC/ETH prices, open trades, daily PnL from injected context.
Be direct, use numbers, give specific entry/SL/TP levels when asked.
When user asks to execute something, use FUNCTION_CALL format.

FUNCTION_CALL format:
FUNCTION_CALL: {"action": "<action>", "params": {<params>}}
Actions: long ({"sym":"BTCUSDT","lots":1}), short ({"sym":"ETHUSDT","lots":1}), close ({"sym":"BTCUSDT"})"""

# Per-chat mode: "general" | "options" | "crypto"
_chat_mode: dict[str, str] = {}

def _llm_reply(chat_id: str, user_msg: str) -> str:
    if not OPENROUTER_KEY:
        return "OpenRouter key not set in .env (OPENROUTER_API_KEY)."

    # Pick system prompt based on chat mode
    mode = _chat_mode.get(chat_id, "general")
    if mode == "options":
        sys_prompt = _SYSTEM_PROMPT_OPTIONS
    elif mode == "crypto":
        sys_prompt = _SYSTEM_PROMPT_CRYPTO
    else:
        sys_prompt = _SYSTEM_PROMPT

    # Build live context
    ctx_parts = ["=== LIVE BOT STATE ==="]
    ctx_parts.append(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    btc = _fetch_price("BTC/USD:USD")
    eth = _fetch_price("ETH/USD:USD")
    ctx_parts.append(f"BTC price: {btc or 'N/A'} | ETH price: {eth or 'N/A'}")

    open_count = len(_tracker.open) if _tracker else 0
    ctx_parts.append(f"Crypto open trades: {open_count}")
    if _tracker and _tracker.open:
        ctx_parts.append(_open_trades_summary())
    else:
        ctx_parts.append("No crypto trades currently open.")

    # Options paper account context
    try:
        engine = get_engine()
        opt_positions = engine.get_open_positions()
        opt_summary   = engine.get_summary()
        if opt_positions:
            ctx_parts.append(f"\nOptions paper positions ({len(opt_positions)}):")
            for p in opt_positions:
                ctx_parts.append(
                    f"  {p['underlying']} {p['strike']}{p['opt_type']} {p['expiry']} "
                    f"{p['action']} | Entry: ₹{p['entry_premium']} | LTP: ₹{p['ltp']} "
                    f"| PnL: ₹{p.get('unrealized_pnl', 0):+.0f} "
                    f"| SL: {p.get('sl') or '—'} | TP: {p.get('tp') or '—'}"
                    f"| Key: {p['key']}"
                )
        else:
            ctx_parts.append("\nNo open options positions.")
        ctx_parts.append(
            f"Options capital: ₹{opt_summary['capital']:,.0f} | "
            f"Open PnL: ₹{opt_summary['open_pnl']:+,.0f} | "
            f"Realized: ₹{opt_summary['realized_pnl']:+,.0f}"
        )
    except Exception:
        pass

    # Virtual exchange context
    try:
        vx_positions = vx.get_positions()
        vx_summary   = vx.summary()
        if vx_positions:
            ctx_parts.append(f"\nVirtual exchange positions ({len(vx_positions)}):")
            for p in vx_positions:
                ctx_parts.append(
                    f"  {p['symbol']} {p['side']} qty:{p['qty']} "
                    f"entry:₹{p['entry']} ltp:₹{p['ltp']} pnl:₹{p['pnl']:+.2f}"
                )
        ctx_parts.append(f"Virtual balance: ₹{vx_summary['balance']:,.0f} | Realized: ₹{vx_summary['realized_pnl']:+.0f}")
    except Exception:
        pass

    today = datetime.now(timezone.utc).date()
    trades_today = _daily_trades.get(today, 0) if _daily_trades else 0
    loss_today   = _daily_loss.get(today, 0.0) if _daily_loss else 0.0
    ctx_parts.append(f"\nToday crypto: {trades_today} trades | Loss: {loss_today:.4f} USD")

    paused = _kill_switch.is_set() if _kill_switch else False
    ctx_parts.append(f"Bot status: {'PAUSED' if paused else 'RUNNING'}")
    ctx_parts.append("=== END STATE ===")

    context_block = "\n".join(ctx_parts)

    history = _conv_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_msg})

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user",   "content": context_block},
        {"role": "assistant", "content": "Got it, I have the live state."},
    ] + history[-MAX_HISTORY:]

    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/sayantan/live-trade",
            },
            json={
                "model":      OPENROUTER_MODEL,
                "messages":   messages,
                "max_tokens": 400,
            },
            timeout=20,
        )
        data  = r.json()
        reply = data["choices"][0]["message"]["content"].strip()
        history.append({"role": "assistant", "content": reply})
        if len(history) > MAX_HISTORY * 2:
            _conv_history[chat_id] = history[-(MAX_HISTORY * 2):]

        # ── Function call handler ─────────────────────────────────────────
        if reply.startswith("FUNCTION_CALL:"):
            return _execute_llm_function(chat_id, reply)

        return reply
    except Exception as e:
        log.warning(f"[BOT] LLM error: {e}")
        return f"LLM error: {e}"


def _execute_llm_function(chat_id: str, reply: str) -> str:
    """Parse and execute a FUNCTION_CALL from the LLM."""
    import json as _json
    try:
        raw  = reply.replace("FUNCTION_CALL:", "").strip()
        data = _json.loads(raw)
        action = data.get("action", "")
        params = data.get("params", {})
    except Exception as e:
        return f"⚠️ Could not parse function call: {e}\nRaw: {reply}"

    engine = get_engine()

    if action == "set_tp":
        key = params.get("key", "")
        tp  = params.get("tp")
        if not key or tp is None:
            return "❌ set_tp needs 'key' and 'tp'."
        ok, msg = engine.set_tp(key, float(tp))
        return msg

    elif action == "set_sl":
        key = params.get("key", "")
        sl  = params.get("sl")
        if not key or sl is None:
            return "❌ set_sl needs 'key' and 'sl'."
        ok, msg = engine.set_sl(key, float(sl))
        return msg

    elif action == "close_opt":
        key = params.get("key", "")
        pos = engine.get_position(key)
        if not pos:
            return f"❌ No open position: {key}"
        from options_bot_commands import _fetch_option_ltp
        from fyers_data import get_fyers
        fyers  = get_fyers()
        ltp    = _fetch_option_ltp(fyers, pos["underlying"], pos["strike"], pos["opt_type"], pos["expiry"])
        if not ltp:
            ltp = pos["ltp"]
        ok, msg = engine.close_position(key, ltp, reason="llm_close")
        return msg

    elif action == "buy_opt":
        underlying = params.get("underlying", "NIFTY").upper()
        strike     = int(params.get("strike", 0))
        opt_type   = params.get("opt_type", "CE").upper()
        expiry     = params.get("expiry", "")
        lots       = int(params.get("lots", 1))
        from options_bot_commands import _fetch_option_ltp
        from fyers_data import get_fyers
        fyers   = get_fyers()
        premium = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        if not premium:
            return f"❌ Could not fetch LTP for {underlying} {strike}{opt_type} {expiry}"
        ok, msg = engine.place_order(underlying, strike, opt_type, expiry, "BUY", lots, premium)
        return msg

    elif action == "sell_opt":
        underlying = params.get("underlying", "NIFTY").upper()
        strike     = int(params.get("strike", 0))
        opt_type   = params.get("opt_type", "CE").upper()
        expiry     = params.get("expiry", "")
        lots       = int(params.get("lots", 1))
        from options_bot_commands import _fetch_option_ltp
        from fyers_data import get_fyers
        fyers   = get_fyers()
        premium = _fetch_option_ltp(fyers, underlying, strike, opt_type, expiry)
        if not premium:
            return f"❌ Could not fetch LTP for {underlying} {strike}{opt_type} {expiry}"
        ok, msg = engine.place_order(underlying, strike, opt_type, expiry, "SELL", lots, premium)
        return msg

    elif action == "opt_status":
        from options_bot_commands import _format_open_options
        return _format_open_options(engine)

    elif action == "opt_pnl":
        s       = engine.get_summary()
        day_pnl = engine.get_daily_pnl()
        return (
            f"💰 <b>Options Paper Account</b>\n"
            f"Capital: ₹{s['capital']:,.0f} | Available: ₹{s['available']:,.0f}\n"
            f"Open PnL: ₹{s['open_pnl']:+,.0f}\n"
            f"Today PnL: ₹{day_pnl:+,.0f}\n"
            f"Total Realized: ₹{s['realized_pnl']:+,.0f}"
        )

    elif action == "long":
        sym  = params.get("sym", "BTCUSDT").upper()
        lots = int(params.get("lots", 1))
        _handle(chat_id, f"/long {sym} {lots}")
        return ""

    elif action == "short":
        sym  = params.get("sym", "BTCUSDT").upper()
        lots = int(params.get("lots", 1))
        _handle(chat_id, f"/short {sym} {lots}")
        return ""

    elif action == "close":
        sym = params.get("sym", "").upper()
        _handle(chat_id, f"/close {sym}")
        return ""

    return f"⚠️ Unknown action: {action}"


# ─── Command router ──────────────────────────────────────────────────────────

def _handle(chat_id: str, text: str):
    text = text.strip()

    # Cancel any in-progress LLM reply for this chat whenever a new message arrives
    import random
    _llm_tokens[chat_id] = random.randint(1, 999999)

    # Auto-trade confirmation check (YES/NO for options auto-trader)
    if _handle_auto_confirm(chat_id, text):
        return

    if handle_options_commands(chat_id, text, _send):
        return

    # Check pending confirmation first
    if _handle_confirm(chat_id, text):
        return

    # Auto convert commands like /chart_BTCUSDT to /chart BTCUSDT
    # Note: /opt* commands use underscores as part of the key format, so skip them
    if text.startswith("/") and "_" in text and not text.startswith("/opt"):
        parts = text.split("_", 1)
        action = parts[0]
        sym = parts[1].upper()
        text = f"{action} {sym}"

    parts = text.split()
    cmd   = parts[0].lower() if parts else ""

    # ── Info commands ────────────────────────────────────────
    if cmd == "/status":
        _purge_stale_positions()
        _send(chat_id, "📊 <b>Open Trades</b>\n\n" + _open_trades_summary())

    elif cmd == "/history":
        limit = 10
        if len(parts) > 1:
            try:
                limit = int(parts[1])
            except ValueError:
                pass
        _send(chat_id, _closed_trades_summary(limit))

    elif cmd == "/daily":
        _send(chat_id, _daily_summary())

    elif cmd == "/price":
        _send(chat_id, _prices_summary())

    elif cmd == "/chart":
        sym = parts[1].upper() if len(parts) > 1 else ""
        if not sym or sym not in SYMBOL_MAP:
            _send(chat_id, "❌ Usage: /chart BTCUSDT  or  /chart ETHUSDT")
            return

        _purge_stale_positions()
        _send(chat_id, f"📊 Fetching data and generating 15m chart for {sym}...")
        
        entry = sl = tp = None
        if _tracker and sym in _tracker.open:
            m = _tracker.open[sym]
            entry = m.get("entry")
            sl = m.get("sl")
            tp = m.get("tp")
            
        path = _generate_market_chart(sym, entry, sl, tp)
        if path:
            caption = f"📊 <b>{sym} 15m Market Chart</b>"
            if entry:
                caption += f"\nActive trade: {SYMBOL_MAP[sym]['ccxt']}\nEntry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}"
            _send_photo(chat_id, path, caption)
        else:
            _send(chat_id, f"❌ Failed to generate chart for {sym}.")

    elif cmd == "/modify":
        sym = parts[1].upper() if len(parts) > 1 else ""
        if not sym or sym not in SYMBOL_MAP:
            _send(chat_id, "❌ Usage: /modify BTCUSDT sl [value] tp [value]\nExamples:\n- /modify BTCUSDT sl 68000\n- /modify BTCUSDT tp 72000 sl 68000")
            return

        _purge_stale_positions()
        if not _tracker or sym not in _tracker.open:
            _send(chat_id, f"❌ No active trade tracked for {sym}.")
            return
            
        new_sl = None
        new_tp = None
        
        for idx in range(2, len(parts) - 1):
            flag = parts[idx].lower()
            val_str = parts[idx + 1]
            if flag == "sl":
                try:
                    new_sl = float(val_str)
                except ValueError:
                    _send(chat_id, f"❌ Invalid SL value: {val_str}")
                    return
            elif flag == "tp":
                try:
                    new_tp = float(val_str)
                except ValueError:
                    _send(chat_id, f"❌ Invalid TP value: {val_str}")
                    return
                    
        if new_sl is None and new_tp is None:
            _send(chat_id, "❌ Please specify sl or tp to modify. Example: /modify BTCUSDT sl 68000")
            return
            
        _update_bracket_orders(chat_id, sym, new_sl, new_tp)

    elif cmd == "/trail":
        sym = parts[1].upper() if len(parts) > 1 else ""
        if not sym or sym not in SYMBOL_MAP:
            _send(chat_id, "❌ Usage: /trail BTCUSDT [trail_distance/percent/off]\nExamples:\n- /trail BTCUSDT 150 (trail by 150 USD)\n- /trail BTCUSDT 0.5% (trail by 0.5%)\n- /trail BTCUSDT off (disable trailing)")
            return

        _purge_stale_positions()
        if not _tracker or sym not in _tracker.open:
            _send(chat_id, f"❌ No active trade tracked for {sym}.")
            return
            
        m = _tracker.open[sym]
        arg = parts[2] if len(parts) > 2 else "auto"
        
        if arg.lower() == "off":
            m["trail_dist"] = None
            from state_manager import save_positions
            save_positions(_tracker.open)
            _send(chat_id, f"🛑 Trailing stop loss disabled for {sym}.")
            return
            
        px = _fetch_price(SYMBOL_MAP[sym]["ccxt"])
        if not px:
            _send(chat_id, "❌ Could not fetch live price to configure trailing.")
            return
            
        if arg.lower() == "auto":
            trail_dist = px * 0.005
        elif arg.endswith("%"):
            try:
                pct = float(arg[:-1]) / 100.0
                trail_dist = px * pct
            except ValueError:
                _send(chat_id, f"❌ Invalid trail percent: {arg}")
                return
        else:
            try:
                trail_dist = float(arg)
            except ValueError:
                _send(chat_id, f"❌ Invalid trail distance: {arg}")
                return
                
        m["trail_dist"] = trail_dist
        if m["side"] == "buy":
            m["highest_px"] = px
        else:
            m["lowest_px"] = px

        # Persist trail config
        from state_manager import save_positions
        save_positions(_tracker.open)
            
        _send(chat_id, f"🏃‍♂️ <b>Trailing Stop Loss Activated for {sym}</b>\nTrail Distance: {trail_dist:.2f} USD")

    elif cmd == "/pause":
        if _kill_switch:
            _kill_switch.set()
            _send(chat_id, "⏸ Trading PAUSED. No new auto entries until /resume.")
            log.info("[BOT] Kill switch SET.")
        else:
            _send(chat_id, "Kill switch not wired.")

    elif cmd == "/resume":
        if _kill_switch:
            _kill_switch.clear()
            _send(chat_id, "▶ Trading RESUMED.")
            log.info("[BOT] Kill switch CLEARED.")
        else:
            _send(chat_id, "Kill switch not wired.")

    # ── Manual trade commands ────────────────────────────────
    elif cmd == "/long":
        sym = parts[1].upper() if len(parts) > 1 else ""
        if sym not in SYMBOL_MAP:
            _send(chat_id, "❌ Usage: /long BTCUSDT [lots/auto] [leverage]\n(Defaults: lots=auto, leverage=default)")
            return
        
        lots_arg = parts[2] if len(parts) > 2 else "auto"
        
        leverage_val = None
        if len(parts) > 3:
            try:
                leverage_val = int(parts[3])
            except ValueError:
                pass
        
        px = _fetch_price(SYMBOL_MAP[sym]["ccxt"])
        if not px:
            _send(chat_id, "❌ Could not fetch price. Try again.")
            return
            
        contract_size = SYMBOL_MAP[sym]["contract_size"]
        lots_desc_type = "auto"
        if lots_arg.lower() == "auto":
            equity = 1000.0
            if _exchange:
                try:
                    b = _exchange.fetch_balance()
                    equity = float(b.get("total", {}).get("USD", 1000.0))
                except Exception:
                    pass
            risk_amount = equity * 0.01
            sl_dist_est = px * 0.005
            lots_val = max(1, round(risk_amount / sl_dist_est / contract_size))
            lots_desc_type = "auto"
        else:
            try:
                lots_val = int(lots_arg)
                lots_desc_type = "manual"
            except ValueError:
                lots_val = DEFAULT_MANUAL_LOTS
                lots_desc_type = "default"
                
        _pending[chat_id] = {
            "action": "ask_sl",
            "params": {
                "sym": sym,
                "side": "buy",
                "px": px,
                "lots": lots_val,
                "leverage": leverage_val,
                "lots_desc_type": lots_desc_type,
            },
            "expires": time.time() + CONFIRM_TIMEOUT * 2
        }
        
        _send(chat_id,
            f"📈 <b>LONG {sym} Initiated</b>\n"
            f"Current Price: {px:.2f}\n\n"
            f"Please reply with your desired Stop Loss (SL) price. You can enter:\n"
            f"- Price: (e.g. <code>68500</code>)\n"
            f"- Percentage: (e.g. <code>0.5%</code>)\n"
            f"- <code>auto</code> for default 0.5% distance"
        )

    elif cmd == "/short":
        sym = parts[1].upper() if len(parts) > 1 else ""
        if sym not in SYMBOL_MAP:
            _send(chat_id, "❌ Usage: /short BTCUSDT [lots/auto] [leverage]\n(Defaults: lots=auto, leverage=default)")
            return
            
        lots_arg = parts[2] if len(parts) > 2 else "auto"
        
        leverage_val = None
        if len(parts) > 3:
            try:
                leverage_val = int(parts[3])
            except ValueError:
                pass
        
        px = _fetch_price(SYMBOL_MAP[sym]["ccxt"])
        if not px:
            _send(chat_id, "❌ Could not fetch price. Try again.")
            return
            
        contract_size = SYMBOL_MAP[sym]["contract_size"]
        lots_desc_type = "auto"
        if lots_arg.lower() == "auto":
            equity = 1000.0
            if _exchange:
                try:
                    b = _exchange.fetch_balance()
                    equity = float(b.get("total", {}).get("USD", 1000.0))
                except Exception:
                    pass
            risk_amount = equity * 0.01
            sl_dist_est = px * 0.005
            lots_val = max(1, round(risk_amount / sl_dist_est / contract_size))
            lots_desc_type = "auto"
        else:
            try:
                lots_val = int(lots_arg)
                lots_desc_type = "manual"
            except ValueError:
                lots_val = DEFAULT_MANUAL_LOTS
                lots_desc_type = "default"
                
        _pending[chat_id] = {
            "action": "ask_sl",
            "params": {
                "sym": sym,
                "side": "sell",
                "px": px,
                "lots": lots_val,
                "leverage": leverage_val,
                "lots_desc_type": lots_desc_type,
            },
            "expires": time.time() + CONFIRM_TIMEOUT * 2
        }
        
        _send(chat_id,
            f"📉 <b>SHORT {sym} Initiated</b>\n"
            f"Current Price: {px:.2f}\n\n"
            f"Please reply with your desired Stop Loss (SL) price. You can enter:\n"
            f"- Price: (e.g. <code>71500</code>)\n"
            f"- Percentage: (e.g. <code>0.5%</code>)\n"
            f"- <code>auto</code> for default 0.5% distance"
        )

    elif cmd == "/close":
        sym = parts[1].upper() if len(parts) > 1 else ""
        if not sym:
            _send(chat_id, "❌ Usage: /close BTCUSDT")
            return
        _purge_stale_positions()
        if not _tracker or sym not in _tracker.open:
            _send(chat_id, f"❌ No open position for {sym}.")
            return
        m = _tracker.open[sym]
        px = _fetch_price(m["ccxt_sym"])
        sign = 1 if m["side"] == "buy" else -1
        upnl = sign * (px - m["entry"]) * m["lots"] * m["contract_size"] if px else 0
        _ask_confirm(chat_id, "close", {"sym": sym},
            f"🔒 CLOSE {sym} {m['side'].upper()}\nEntry: {m['entry']:.2f} | Now: {px}\nuPnL: {upnl:+.4f} USD")

    elif cmd == "/closeall":
        _purge_stale_positions()
        if not _tracker or not _tracker.open:
            _send(chat_id, "No open positions.")
            return
        syms = list(_tracker.open.keys())
        _ask_confirm(chat_id, "closeall", {},
            f"🔒 CLOSE ALL {len(syms)} position(s): {', '.join(syms)}")

    elif cmd == "/emergency":
        # No confirmation — instant close all
        _send(chat_id, "🚨 <b>EMERGENCY CLOSE ALL</b> — executing now...")
        log.warning("[BOT] EMERGENCY triggered from Telegram.")
        _exec_closeall(chat_id)

    elif cmd == "/funds":
        try:
            from fyers_data import get_fyers
            fyers = get_fyers()
            if fyers is None:
                _send(chat_id,
                    "❌ Fyers not authenticated.\n"
                    "Run python fyers_auth.py first.")
                
                return
            data = fyers.funds()
            _send(chat_id, str(data))
                  
        except Exception as e:
         _send(chat_id, f"❌ Failed to fetch funds: {e}")          
        

    elif cmd == "/options":
        _send(chat_id, "🔍 Scanning options market... (10–20s)")
        def _do_scan():
            try:
                from fyers_data import get_fyers
                from options_scanner import scan_options

                fyers = get_fyers()
                if fyers is None:
                    _send(chat_id,
                        "❌ Fyers not authenticated.\n"
                        "Run python fyers_auth.py first."
                    )
                    return

                result = scan_options(fyers)

                # Split if > 4096 chars (Telegram limit)
                if len(result) <= 4096:
                    _send(chat_id, result)
                else:
                    chunks = []
                    current = ""
                    for line in result.split("\n"):
                        if len(current) + len(line) + 1 > 4000:
                            chunks.append(current)
                            current = line + "\n"
                        else:
                            current += line + "\n"
                    if current:
                        chunks.append(current)
                    for chunk in chunks:
                        _send(chat_id, chunk)

            except Exception as e:
                log.error(f"[BOT] /options error: {e}")
                _send(chat_id, f"❌ Options scan failed: {e}")

        t = threading.Thread(target=_do_scan, daemon=True)
        t.start()
        t.join(timeout=40)   # hard 40s timeout
        if t.is_alive():
            _send(chat_id, "❌ Options scan timed out (Fyers API slow). Try again in a moment.")

    elif cmd == "/stocks":
        try:
            from fyers_data import get_fyers
            from stock_scanner import scan_stocks
            fyers = get_fyers()
            if fyers is None:
                _send(chat_id, "❌ Fyers not authenticated.")
                return
            result = scan_stocks(fyers)
            _send(chat_id, result)
        except Exception as e:
            log.error(f"[BOT] /stocks error: {e}")
            _send(chat_id, f"❌ Stocks scan failed: {e}")

    elif cmd == "/mode":
        mode_arg = parts[1].lower() if len(parts) > 1 else ""
        if mode_arg in ("options", "opt"):
            _chat_mode[chat_id] = "options"
            _conv_history.pop(chat_id, None)   # clear history on mode switch
            _send(chat_id,
                "📊 <b>Switched to OPTIONS mode</b>\n"
                "I am now your dedicated NIFTY/BANKNIFTY/SENSEX options analyst.\n"
                "You can ask me about PDH/PDL, PCR, OI, Greeks, strategies — "
                "and tell me to set SL/TP or place trades directly.\n\n"
                "Switch back anytime: /mode crypto or /mode general")
        elif mode_arg in ("crypto", "btc", "eth"):
            _chat_mode[chat_id] = "crypto"
            _conv_history.pop(chat_id, None)
            _send(chat_id,
                "₿ <b>Switched to CRYPTO mode</b>\n"
                "I am now your BTC/ETH SMC strategy assistant.\n"
                "Ask me about setups, liquidity grabs, BOS, FVGs — "
                "or tell me to go long/short/close.\n\n"
                "Switch back anytime: /mode options or /mode general")
        elif mode_arg in ("general", "all", "reset"):
            _chat_mode[chat_id] = "general"
            _conv_history.pop(chat_id, None)
            _send(chat_id,
                "🤖 <b>Switched to GENERAL mode</b>\n"
                "I cover all markets: crypto, options, and virtual stocks.")
        else:
            current = _chat_mode.get(chat_id, "general")
            _send(chat_id,
                f"Current mode: <b>{current.upper()}</b>\n\n"
                "Switch with:\n"
                "/mode options — NIFTY/BANKNIFTY options analyst\n"
                "/mode crypto  — BTC/ETH SMC trader\n"
                "/mode general — all markets")

    elif cmd == "/autoscan":
        # Manual trigger: run auto-scan now for all indices
        _send(chat_id, "🔍 Running options auto-scan now...")
        def _run_scan():
            from options_scanner import get_signal, SYMBOLS
            fyers_client = get_fyers()
            if not fyers_client:
                _send(chat_id, "❌ Fyers not authenticated.")
                return
            found = 0
            for index in SYMBOLS:
                try:
                    sig = get_signal(fyers_client, index)
                    if sig is None:
                        _send(chat_id, f"  {index}: no signal / weak.")
                        continue
                    found += 1
                    strategy  = sig.get("strategy", "")
                    trade_key = (index, strategy)
                    sig["_expires"] = time.time() + AUTO_CONFIRM_TIMEOUT
                    with _auto_lock:
                        _auto_pending[chat_id] = sig
                    _send(chat_id, _format_signal_confirm(sig))
                    return  # one signal at a time
                except Exception as e:
                    _send(chat_id, f"  {index}: error — {e}")
            if found == 0:
                _send(chat_id, "No signals found across NIFTY / BANKNIFTY / SENSEX right now.")
        threading.Thread(target=_run_scan, daemon=True).start()


    elif cmd == "/paperbuy":
        # Usage: /paperbuy SYMBOL QTY [PRICE]
        # If PRICE is omitted, fetches live LTP from Fyers
        if len(parts) < 3:
            _send(chat_id,
                "Usage: /paperbuy SYMBOL QTY [PRICE]\n"
                "Ex: /paperbuy NSE:RELIANCE-EQ 10\n"
                "Ex: /paperbuy NSE:RELIANCE-EQ 10 2500\n"
                "If price is omitted, live LTP is fetched automatically.")
            return

        symbol = parts[1].upper()
        try:
            qty = int(parts[2])
        except ValueError:
            _send(chat_id, "❌ QTY must be a whole number.")
            return

        if len(parts) >= 4:
            try:
                price = float(parts[3])
            except ValueError:
                _send(chat_id, "❌ PRICE must be a number.")
                return
        else:
            # Fetch live LTP from Fyers
            try:
                from fyers_data import get_fyers, get_quotes
                fyers = get_fyers()
                quotes = get_quotes(fyers, [symbol]) if fyers else None
                if not quotes:
                    _send(chat_id, f"❌ Could not fetch live price for {symbol}. Pass price manually: /paperbuy {symbol} {qty} PRICE")
                    return
                price = float(quotes[0]["v"]["lp"])
            except Exception as e:
                _send(chat_id, f"❌ Price fetch failed: {e}\nPass price manually: /paperbuy {symbol} {qty} PRICE")
                return

        ok, msg = vx.buy(symbol, qty, price)
        _send(chat_id, f"{'✅' if ok else '❌'} {msg}")

    elif cmd == "/papersell":
        # Usage: /papersell SYMBOL QTY [PRICE]
        if len(parts) < 3:
            _send(chat_id,
                "Usage: /papersell SYMBOL QTY [PRICE]\n"
                "Ex: /papersell NSE:RELIANCE-EQ 10\n"
                "If price is omitted, live LTP is fetched automatically.")
            return

        symbol = parts[1].upper()
        try:
            qty = int(parts[2])
        except ValueError:
            _send(chat_id, "❌ QTY must be a whole number.")
            return

        if len(parts) >= 4:
            try:
                price = float(parts[3])
            except ValueError:
                _send(chat_id, "❌ PRICE must be a number.")
                return
        else:
            try:
                from fyers_data import get_fyers, get_quotes
                fyers = get_fyers()
                quotes = get_quotes(fyers, [symbol]) if fyers else None
                if not quotes:
                    _send(chat_id, f"❌ Could not fetch live price for {symbol}. Pass price manually: /papersell {symbol} {qty} PRICE")
                    return
                price = float(quotes[0]["v"]["lp"])
            except Exception as e:
                _send(chat_id, f"❌ Price fetch failed: {e}\nPass price manually: /papersell {symbol} {qty} PRICE")
                return

        ok, msg = vx.sell(symbol, qty, price)
        _send(chat_id, f"{'✅' if ok else '❌'} {msg}")

    elif cmd == "/paperclose":
        # Usage: /paperclose SYMBOL [PRICE]
        if len(parts) < 2:
            _send(chat_id,
                "Usage: /paperclose SYMBOL [PRICE]\n"
                "Ex: /paperclose NSE:RELIANCE-EQ\n"
                "If price is omitted, live LTP is fetched automatically.")
            return

        symbol = parts[1].upper()

        if len(parts) >= 3:
            try:
                exit_price = float(parts[2])
            except ValueError:
                _send(chat_id, "❌ PRICE must be a number.")
                return
        else:
            try:
                from fyers_data import get_fyers, get_quotes
                fyers = get_fyers()
                quotes = get_quotes(fyers, [symbol]) if fyers else None
                if not quotes:
                    # Fall back to last known LTP from position
                    positions = vx.get_positions()
                    pos = next((p for p in positions if p["symbol"] == symbol), None)
                    if pos:
                        exit_price = pos["ltp"]
                        _send(chat_id, f"⚠️ Using last known LTP ₹{exit_price} (live fetch failed)")
                    else:
                        _send(chat_id, f"❌ No open position for {symbol}.")
                        return
                else:
                    exit_price = float(quotes[0]["v"]["lp"])
            except Exception as e:
                _send(chat_id, f"❌ Price fetch failed: {e}")
                return

        ok, result = vx.close_position(symbol, exit_price)
        if ok:
            pnl = result
            emoji = "🟢" if pnl >= 0 else "🔴"
            _send(chat_id, f"{emoji} <b>CLOSED {symbol}</b>\nExit: ₹{exit_price} | PnL: ₹{pnl:+.2f}")
        else:
            _send(chat_id, f"❌ {result}")

    elif cmd == "/paperstatus":
        positions = vx.get_positions()
        s = vx.summary()
        if not positions:
            _send(chat_id,
                f"📭 No open virtual positions.\n\n"
                f"💰 Balance: ₹{s['balance']:,.2f} | Realized PnL: ₹{s['realized_pnl']:+.2f}")
            return

        lines = ["📊 <b>Virtual Positions</b>\n"]
        for p in positions:
            pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
            lines.append(
                f"{pnl_emoji} <b>{p['symbol']}</b> | {p['side']}\n"
                f"   Qty: {p['qty']} | Entry: ₹{p['entry']} | LTP: ₹{p['ltp']}\n"
                f"   PnL: <b>₹{p['pnl']:+.2f}</b>"
            )
        lines.append(
            f"\n💰 Balance: ₹{s['balance']:,.2f}\n"
            f"Open PnL: ₹{s['open_pnl']:+.2f} | Realized: ₹{s['realized_pnl']:+.2f}"
        )
        _send(chat_id, "\n\n".join(lines))

    elif cmd == "/holdings":
        from fyers_data import get_fyers
        fyers = get_fyers()
        data = fyers.holdings()
        _send(chat_id, str(data))      

    elif cmd == "/positions":
        from fyers_data import get_fyers
        fyers = get_fyers()
        data = fyers.positions()
        _send(chat_id, str(data)) 

    elif cmd == "/paperbalance":
        s = vx.summary()

        _send(
            chat_id,
            f"💰 Virtual Account\n\n"
            f"Balance: ₹{s['balance']:.2f}\n"
            f"Equity: ₹{s['equity']:.2f}\n"
            f"Open PnL: ₹{s['open_pnl']:.2f}\n"
            f"Realized PnL: ₹{s['realized_pnl']:.2f}\n"
            f"Open Positions: {s['open_positions']}"
        )

    elif cmd == "/paperpositions":
        positions = vx.get_positions()

        if not positions:
            _send(chat_id, "📭 No open positions.")
            return

        msg = "📊 Open Positions\n\n"

        for p in positions:
            msg += (
                f"{p['symbol']}\n"
                f"Side: {p['side']}\n"
                f"Qty: {p['qty']}\n"
                f"Entry: ₹{p['entry']}\n"
                f"LTP: ₹{p['ltp']}\n"
                f"PnL: ₹{p['pnl']:.2f}\n\n"
            )

        _send(chat_id, msg)     

    elif cmd == "/help":
        _send(chat_id,
            "🤖 <b>Commands</b>\n\n"
            "<b>Info</b>\n"
            "/status     — open trades + uPnL + controls\n"
            "/daily      — today stats + state\n"
            "/history    — recent closed trades (e.g. /history 10)\n"
            "/price      — live BTC + ETH\n"
            "/chart SYMBOL — show live 15m chart of position\n"
            "/funds      — available funds & margin"
            "/holdings   — stock portfolio"
            "/positions  — open F&O positions"
            "/options    — scan Nifty/BankNifty/Sensex options\n\n"
            "<b>Bot Control</b>\n"
            "/pause      — stop auto entries\n"
            "/resume     — restart auto entries\n\n"
            "<b>Manual Trading</b>\n"
            "/long BTCUSDT  — manual long (interactive SL/TP)\n"
            "/short ETHUSDT — manual short (interactive SL/TP)\n"
            "/modify BTCUSDT sl [val] tp [val] — adjust SL/TP of active trade\n"
            "/trail BTCUSDT [val/off] — trail stop loss (USD or %)\n"
            "/close BTCUSDT — close position\n"
            "/closeall      — close all positions\n"
            "/emergency     — instant close all (no confirm)\n\n"
            "Or just <b>ask anything</b> about market / trades.\n\n"
            "<b>Mode Switching</b>\n"
            "/mode options — switch to options analyst mode\n"
            "/mode crypto  — switch to crypto trader mode\n"
            "/mode general — switch back to all markets\n\n"
            "<b>Options Paper Trading</b>\n"
            "/opthelp    — all options commands\n"
            "/optstatus  — open option positions\n"
            "/optpnl     — options account PnL\n"
            "/autoscan   — run options signal scan now (sends confirm)\n\n"
            "<b>Virtual Exchange (Indian Stocks)</b>\n"
            "/paperbuy SYMBOL QTY [PRICE]   — virtual buy (live LTP if no price)\n"
            "/papersell SYMBOL QTY [PRICE]  — virtual sell/short\n"
            "/paperclose SYMBOL [PRICE]     — close virtual position\n"
            "/paperstatus                   — open virtual positions\n"
            "/paperbalance                  — virtual account summary\n"
        )

    else:
        # Freeform → LLM
        # Assign a token for this request — if a newer message arrives, this reply is stale
        import random
        token = random.randint(1, 999999)
        _llm_tokens[chat_id] = token
        _send(chat_id, "⏳ thinking...")
        reply = _llm_reply(chat_id, text)
        # Only send if no newer message has arrived since we started
        if _llm_tokens.get(chat_id) == token:
            _send(chat_id, reply)
        else:
            log.info(f"[BOT] LLM reply discarded for {chat_id} — superseded by newer message.")


# ─── Poll loop ───────────────────────────────────────────────────────────────

def _run():
    if not BOT_TOKEN:
        log.warning("[BOT] TELEGRAM_BOT_TOKEN not set. Bot disabled.")
        return

    log.info(f"[BOT] Starting. Model: {OPENROUTER_MODEL}")
    offset = 0
    _send(CHAT_ID, "🚀 <b>Live Trader started.</b>\nType /help for commands.")

    while True:
        try:
            updates = _get_updates(offset)
            for u in updates:
                offset = u["update_id"] + 1
                msg    = u.get("message", {})
                text   = msg.get("text", "").strip()
                cid    = str(msg.get("chat", {}).get("id", ""))
                if not text or not cid:
                    continue
                if CHAT_ID and cid != str(CHAT_ID):
                    log.warning(f"[BOT] Ignored msg from unknown chat_id={cid}")
                    continue
                log.info(f"[BOT] Received: '{text}' from {cid}")
                threading.Thread(target=_handle, args=(cid, text), daemon=True).start()
        except Exception as e:
            log.warning(f"[BOT] Poll loop error: {e}")

        time.sleep(POLL_INTERVAL)


# ─── Options Auto-Trader ─────────────────────────────────────────────────────
#
# Runs every 5 minutes during market hours (09:15 – 15:25 IST).
# On each tick:
#   1. Calls get_signal() for each index
#   2. If a NEW signal is found (not already pending / already traded this session):
#      → Sends a confirmation request to Telegram with full details
#      → Waits up to 45s for YES/NO
#      → On YES → places paper trade into OptionsPaperEngine + sets SL/TP
#      → On NO / timeout → cancels silently
#
# Tracks which (index, strategy) combos have been traded in this session
# so we don't spam the same signal repeatedly.
# ─────────────────────────────────────────────────────────────────────────────

AUTO_SCAN_INTERVAL  = 300    # seconds between scans (5 minutes)
AUTO_CONFIRM_TIMEOUT = 45    # seconds to wait for user YES
MARKET_OPEN_H  = 9
MARKET_OPEN_M  = 15
MARKET_CLOSE_H = 15
MARKET_CLOSE_M = 25

# Per-session trade memory: set of (index, strategy) already confirmed/traded today
_auto_traded_today: set = set()
_auto_pending: dict     = {}   # chat_id → signal dict (waiting for confirm)
_auto_lock = threading.Lock()


def _is_market_hours() -> bool:
    """Returns True if current IST time is within market hours."""
    import pytz
    try:
        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist)
        open_ok  = (now.hour, now.minute) >= (MARKET_OPEN_H,  MARKET_OPEN_M)
        close_ok = (now.hour, now.minute) <= (MARKET_CLOSE_H, MARKET_CLOSE_M)
        return now.weekday() < 5 and open_ok and close_ok
    except ImportError:
        # pytz not available — fallback: always allow (user can /pause to stop)
        return True


def _format_signal_confirm(sig: dict) -> str:
    """Format signal as a Telegram confirm message."""
    index    = sig["index"]
    bias     = sig["bias"].upper()
    strength = sig["strength"].upper()
    strategy = sig.get("strategy", "")
    expiry   = sig.get("expiry", "?")
    spot     = sig.get("spot", 0)
    pdh      = sig.get("pdh")
    pdl      = sig.get("pdl")
    vix      = sig.get("vix")
    pcr      = sig.get("pcr")
    ce_wall  = sig.get("ce_wall")
    pe_wall  = sig.get("pe_wall")
    max_pain = sig.get("max_pain")

    lines = [
        f"🤖 <b>AUTO-TRADE SIGNAL</b>  |  {index}",
        f"Bias: <b>{bias}</b> [{strength}]  |  Spot: {spot:.1f}",
        "",
    ]

    if pdh and pdl:
        lines.append(f"PDH: {pdh:.1f}  |  PDL: {pdl:.1f}")
    if vix:
        lines.append(f"VIX: {vix:.1f}  |  PCR: {pcr or 'N/A'}")
    if ce_wall or pe_wall:
        lines.append(f"CE Wall: {ce_wall or 'N/A'}  |  PE Wall: {pe_wall or 'N/A'}")
    if max_pain:
        lines.append(f"Max Pain: {max_pain}")

    lines.append("")

    if strategy in ("buy_ce", "buy_pe"):
        opt_type  = sig.get("opt_type", "")
        strike    = sig.get("strike", "?")
        premium   = sig.get("premium", 0)
        sl_p      = sig.get("sl_premium", 0)
        tp_p      = sig.get("tp_premium", 0)
        lots      = sig.get("lots", 1)
        lot_size  = LOT_SIZE_MAP.get(index, 50)
        cost      = round(premium * lot_size * lots)
        action_emoji = "📈" if opt_type == "CE" else "📉"
        lines += [
            f"{action_emoji} <b>BUY {strike}{opt_type} {expiry}</b>",
            f"Premium: ₹{premium}  |  Lots: {lots} (qty {lots*lot_size})",
            f"Cost: ₹{cost}",
            f"SL: ₹{sl_p}  |  TP: ₹{tp_p}  [1:2 RR]",
        ]

    elif strategy == "straddle":
        strike    = sig.get("strike", "?")
        ce_p      = sig.get("ce_premium", 0)
        pe_p      = sig.get("pe_premium", 0)
        total_p   = sig.get("premium", 0)
        sl_pts    = sig.get("sl_pts", "?")
        lines += [
            f"🔀 <b>SELL STRADDLE — {strike}CE + {strike}PE  {expiry}</b>",
            f"CE premium: ₹{ce_p}  |  PE premium: ₹{pe_p}",
            f"Total collected: ₹{total_p}/lot",
            f"SL: exit if spot moves >{sl_pts} pts",
        ]

    elif strategy == "strangle":
        ce_s   = sig.get("ce_strike", "?")
        pe_s   = sig.get("pe_strike", "?")
        ce_p   = sig.get("ce_premium", 0)
        pe_p   = sig.get("pe_premium", 0)
        total_p= sig.get("premium", 0)
        sl_pts = sig.get("sl_pts", "?")
        lines += [
            f"🔀 <b>SELL STRANGLE — {ce_s}CE + {pe_s}PE  {expiry}</b>",
            f"CE premium: ₹{ce_p}  |  PE premium: ₹{pe_p}",
            f"Total collected: ₹{total_p}/lot",
            f"SL: exit if spot moves >{sl_pts} pts from ATM",
        ]

    lines += [
        "",
        f"⏳ Reply <b>YES</b> to place or <b>NO</b> to skip  (expires {AUTO_CONFIRM_TIMEOUT}s)",
    ]
    return "\n".join(lines)


# Lot sizes for the confirm-message cost display. Single-sourced from the
# engine so this can't drift from the actual trade sizing.
from options_paper_engine import LOT_SIZE as LOT_SIZE_MAP


def _place_auto_trade(sig: dict):
    """Execute the confirmed auto-trade into OptionsPaperEngine."""
    engine   = get_engine()
    index    = sig["index"]
    expiry   = sig.get("expiry", "")
    strategy = sig.get("strategy", "")
    lots     = sig.get("lots", 1)

    if strategy in ("buy_ce", "buy_pe"):
        ok, msg = engine.place_order(
            underlying  = index,
            strike      = sig["strike"],
            opt_type    = sig["opt_type"],
            expiry      = expiry,
            action      = "BUY",
            lots        = lots,
            premium     = sig["premium"],
            strategy_tag= "auto",
            leg_tag     = strategy,
        )
        if ok:
            # Set SL and TP
            from options_paper_engine import _pos_key
            key = _pos_key(index, sig["strike"], sig["opt_type"], expiry)
            engine.set_sl(key, sig["sl_premium"])
            engine.set_tp(key, sig["tp_premium"])
        return ok, msg

    elif strategy == "straddle":
        strike = sig["strike"]
        msgs   = []
        all_ok = True
        for opt_type, premium in [("CE", sig["ce_premium"]), ("PE", sig["pe_premium"])]:
            ok, msg = engine.place_order(
                underlying  = index,
                strike      = strike,
                opt_type    = opt_type,
                expiry      = expiry,
                action      = "SELL",
                lots        = lots,
                premium     = premium,
                strategy_tag= "auto_straddle",
                leg_tag     = f"short_{opt_type.lower()}",
            )
            msgs.append(msg)
            if ok:
                # SELL: SL = premium rises 50% (loss), TP = premium falls 50% (profit)
                from options_paper_engine import _pos_key
                key = _pos_key(index, strike, opt_type, expiry)
                engine.set_sl(key, round(premium * 1.50, 2))
                engine.set_tp(key, round(premium * 0.50, 2))
            else:
                all_ok = False
        return all_ok, "\n".join(msgs)

    elif strategy == "strangle":
        msgs   = []
        all_ok = True
        for opt_type, strike, premium in [
            ("CE", sig["ce_strike"], sig["ce_premium"]),
            ("PE", sig["pe_strike"], sig["pe_premium"]),
        ]:
            ok, msg = engine.place_order(
                underlying  = index,
                strike      = strike,
                opt_type    = opt_type,
                expiry      = expiry,
                action      = "SELL",
                lots        = lots,
                premium     = premium,
                strategy_tag= "auto_strangle",
                leg_tag     = f"short_{opt_type.lower()}",
            )
            msgs.append(msg)
            if ok:
                # SELL: SL = premium rises 50% (loss), TP = premium falls 50% (profit)
                from options_paper_engine import _pos_key
                key = _pos_key(index, strike, opt_type, expiry)
                engine.set_sl(key, round(premium * 1.50, 2))
                engine.set_tp(key, round(premium * 0.50, 2))
            else:
                all_ok = False
        return all_ok, "\n".join(msgs)

    return False, "Unknown strategy"


def _handle_auto_confirm(chat_id: str, text: str) -> bool:
    """
    Called from _handle() — checks if user replied YES/NO to an auto-trade.
    Returns True if the message was consumed.
    """
    with _auto_lock:
        sig = _auto_pending.get(chat_id)
    if not sig:
        return False

    expires = sig.get("_expires", 0)
    word    = text.strip().upper()

    if time.time() > expires:
        with _auto_lock:
            _auto_pending.pop(chat_id, None)
        # silently expired — don't respond (user may send unrelated message)
        return False

    if word == "YES":
        with _auto_lock:
            _auto_pending.pop(chat_id, None)
        _send(chat_id, "⏳ Placing trade...")
        ok, msg = _place_auto_trade(sig)
        status  = "✅ Trade placed!" if ok else "❌ Trade failed"
        _send(chat_id, f"{status}\n\n{msg}")
        if ok:
            with _auto_lock:
                _auto_traded_today.add((sig["index"], sig.get("strategy", "")))
        return True

    elif word == "NO":
        with _auto_lock:
            _auto_pending.pop(chat_id, None)
        _send(chat_id, f"❌ Skipped {sig['index']} auto-trade.")
        with _auto_lock:
            # Mark as skipped so we don't re-alert same signal this session
            _auto_traded_today.add((sig["index"], sig.get("strategy", "")))
        return True

    return False   # not YES/NO — pass through to other handlers


def _auto_trader_loop():
    """
    Background thread: scans every 5 minutes during market hours.
    Sends confirmation messages to Telegram for each new signal.
    Resets traded-today set at midnight.
    """
    from options_scanner import get_signal, SYMBOLS
    log.info("[AUTO] Options auto-trader loop started.")
    last_day = None

    while True:
        time.sleep(AUTO_SCAN_INTERVAL)

        # Reset session memory at midnight
        today = datetime.now().date()
        if last_day and last_day != today:
            with _auto_lock:
                _auto_traded_today.clear()
                _auto_pending.clear()
            log.info("[AUTO] New day — cleared auto-trade session memory.")
        last_day = today

        if not _is_market_hours():
            log.debug("[AUTO] Outside market hours, skipping scan.")
            continue

        if not CHAT_ID:
            log.warning("[AUTO] CHAT_ID not set — cannot send signals.")
            continue

        fyers_client = get_fyers()
        if fyers_client is None:
            log.warning("[AUTO] Fyers not authenticated — skipping auto-scan.")
            continue

        for index in SYMBOLS:
            try:
                # Skip if already traded/skipped this index+direction today
                # (we check per-strategy inside the loop below)
                sig = get_signal(fyers_client, index)
                if sig is None:
                    log.debug(f"[AUTO] {index}: no signal or weak.")
                    continue

                strategy = sig.get("strategy", "")

                # Directional only: auto-trade BUY CE / BUY PE signals.
                # Sideways SELL straddle/strangle stay scan-only (use /options).
                if strategy not in ("buy_ce", "buy_pe"):
                    log.info(f"[AUTO] {index}: {strategy} is non-directional — skipping (scan-only).")
                    continue

                trade_key = (index, strategy)

                with _auto_lock:
                    if trade_key in _auto_traded_today:
                        log.debug(f"[AUTO] {index} {strategy}: already handled today.")
                        continue
                    # Also skip if there's already a pending confirmation for this
                    already_pending = any(
                        p.get("index") == index and p.get("strategy") == strategy
                        for p in _auto_pending.values()
                    )
                    if already_pending:
                        continue

                log.info(f"[AUTO] {index}: {sig['bias'].upper()} {strategy} signal — sending to Telegram.")

                sig["_expires"] = time.time() + AUTO_CONFIRM_TIMEOUT

                with _auto_lock:
                    _auto_pending[CHAT_ID] = sig

                _send(CHAT_ID, _format_signal_confirm(sig))

                # Wait AUTO_CONFIRM_TIMEOUT seconds for response before moving on
                time.sleep(AUTO_CONFIRM_TIMEOUT + 2)

                # If still pending (no YES/NO received), auto-cancel
                with _auto_lock:
                    pending = _auto_pending.get(CHAT_ID)
                    if pending and pending.get("index") == index and pending.get("strategy") == strategy:
                        _auto_pending.pop(CHAT_ID, None)
                        _auto_traded_today.add(trade_key)
                        log.info(f"[AUTO] {index} signal timed out — skipped.")
                        _send(CHAT_ID, f"⏰ Auto-trade for <b>{index}</b> timed out. Skipped.")

            except Exception as e:
                log.warning(f"[AUTO] {index} scan error: {e}")


def start(tracker, daily_trades, daily_loss, exchange, kill_switch):
    init(tracker, daily_trades, daily_loss, exchange, kill_switch)
    engine = get_engine()
    start_monitor(get_fyers, engine, lambda msg: _send(CHAT_ID, msg))

    # Auto-options trader thread
    threading.Thread(target=_auto_trader_loop, name="OptionsAutoTrader", daemon=True).start()
    log.info("[AUTO] Options auto-trader thread started.")

    t = threading.Thread(target=_run, name="TelegramBot", daemon=True)
    t.start()
    log.info("[BOT] Daemon thread started.")
    return t