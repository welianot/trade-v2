"""
bot_server.py
=============
Telegram bot for live_trade.py control + Indian options scanning.

Commands:
  /status   — show open positions + daily stats
  /pause    — pause signal scanning (kill switch ON)
  /resume   — resume signal scanning (kill switch OFF)
  /close    — close all open positions on exchange
  /daily    — today's trade count + PnL summary
  /equity   — current account balance
  /options  — scan Nifty/BankNifty/Sensex options (Fyers data)
  /help     — list all commands

Run via live_trade.py → start_bot(tracker, daily_trades, daily_loss, ex, kill_switch)
"""

import logging
import threading
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ─── Load .env ────────────────────────────────────────────────────────────────

def _load_env():
    env = {}
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


# ─── COMMAND HANDLERS ────────────────────────────────────────────────────────

async def _status(update, context):
    tracker     = context.bot_data["tracker"]
    daily_trades = context.bot_data["daily_trades"]
    daily_loss   = context.bot_data["daily_loss"]
    ex           = context.bot_data["ex"]
    kill_switch  = context.bot_data["kill_switch"]

    day_key      = datetime.now(timezone.utc).date()
    trades_today = daily_trades.get(day_key, 0)
    loss_today   = daily_loss.get(day_key, 0.0)
    paused       = "⏸ PAUSED" if kill_switch.is_set() else "▶ RUNNING"

    with tracker._lock:
        open_snap = dict(tracker.open)

    lines = [f"<b>📊 Bot Status</b>  |  {paused}"]
    lines.append(f"Today: {trades_today} trades | Loss: ${loss_today:.4f}")

    if not open_snap:
        lines.append("\nNo open positions.")
    else:
        lines.append(f"\n<b>Open positions ({len(open_snap)}):</b>")
        for sym_key, m in open_snap.items():
            try:
                px   = float(ex.fetch_ticker(m["ccxt_sym"])["last"])
                sign = 1 if m["side"] == "buy" else -1
                upnl = sign * (px - m["entry"]) * m["lots"] * m["contract_size"]
                lines.append(
                    f"  • {sym_key} {m['side'].upper()} x{m['lots']}\n"
                    f"    Entry: {m['entry']:.2f} | Now: {px:.2f} | uPnL: {upnl:+.4f} USD"
                )
            except Exception:
                lines.append(f"  • {sym_key} {m['side'].upper()} | price N/A")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _pause(update, context):
    kill_switch = context.bot_data["kill_switch"]
    kill_switch.set()
    log.info("[BOT] Kill switch ON via /pause")
    await update.message.reply_text("⏸ <b>Bot PAUSED.</b> No new entries until /resume.", parse_mode="HTML")


async def _resume(update, context):
    kill_switch = context.bot_data["kill_switch"]
    kill_switch.clear()
    log.info("[BOT] Kill switch OFF via /resume")
    await update.message.reply_text("▶ <b>Bot RESUMED.</b> Signal scanning active.", parse_mode="HTML")


async def _close_all(update, context):
    tracker = context.bot_data["tracker"]
    ex      = context.bot_data["ex"]

    with tracker._lock:
        open_snap = dict(tracker.open)

    if not open_snap:
        await update.message.reply_text("No open positions to close.")
        return

    await update.message.reply_text(f"⚠️ Closing {len(open_snap)} position(s)...")

    results = []
    for sym_key, m in open_snap.items():
        ccxt_sym  = m["ccxt_sym"]
        close_side = "sell" if m["side"] == "buy" else "buy"
        try:
            # Cancel bracket orders first
            try:
                open_orders = ex.fetch_open_orders(ccxt_sym)
                for o in open_orders:
                    ex.cancel_order(o["id"], ccxt_sym)
            except Exception:
                pass
            # Market close
            ex.create_order(ccxt_sym, "market", close_side, m["lots"],
                            params={"reduce_only": True})
            results.append(f"✅ {sym_key} closed")
            log.info(f"[BOT] /close: {sym_key} market closed")
        except Exception as e:
            results.append(f"❌ {sym_key} failed: {e}")
            log.error(f"[BOT] /close {sym_key} error: {e}")

    await update.message.reply_text("\n".join(results))


async def _daily(update, context):
    daily_trades = context.bot_data["daily_trades"]
    daily_loss   = context.bot_data["daily_loss"]
    day_key      = datetime.now(timezone.utc).date()

    trades = daily_trades.get(day_key, 0)
    loss   = daily_loss.get(day_key, 0.0)

    await update.message.reply_text(
        f"<b>📅 Today  ({day_key})</b>\n"
        f"Trades: {trades}\n"
        f"Realized loss: ${loss:.4f} USD",
        parse_mode="HTML",
    )


async def _equity(update, context):
    ex = context.bot_data["ex"]
    try:
        b      = ex.fetch_balance()
        equity = float(b.get("total", {}).get("USD", 0))
        await update.message.reply_text(f"💰 <b>Equity:</b> ${equity:.2f} USD", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not fetch balance: {e}")


async def _options(update, context):
    await update.message.reply_text("🔍 Scanning options market... (10–20s)")
    try:
        from fyers_data import get_fyers
        from options_scanner import scan_options

        fyers = get_fyers()
        if fyers is None:
            await update.message.reply_text(
                "❌ Fyers not authenticated.\n"
                "Run <code>python fyers_auth.py</code> first.",
                parse_mode="HTML",
            )
            return

        result = scan_options(fyers)

        # Split if > 4096 chars (Telegram limit)
        if len(result) <= 4096:
            await update.message.reply_text(result, parse_mode="HTML")
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
                await update.message.reply_text(chunk, parse_mode="HTML")

    except Exception as e:
        log.error(f"[BOT] /options error: {e}")
        await update.message.reply_text(f"❌ Options scan failed: {e}")


async def _help(update, context):
    msg = (
        "<b>🤖 Bot Commands</b>\n\n"
        "/status   — open positions + daily stats\n"
        "/pause    — stop new entries\n"
        "/resume   — resume entries\n"
        "/close    — close ALL open positions\n"
        "/daily    — today's trade + loss summary\n"
        "/equity   — account balance\n"
        "/options  — scan Nifty/BankNifty/Sensex options\n"
        "/help     — this message"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ─── BOT RUNNER (background thread) ──────────────────────────────────────────

def _run_bot(token, tracker, daily_trades, daily_loss, ex, kill_switch):
    import asyncio
    from telegram.ext import ApplicationBuilder, CommandHandler

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(token).build()

    # Share state via bot_data
    app.bot_data["tracker"]      = tracker
    app.bot_data["daily_trades"] = daily_trades
    app.bot_data["daily_loss"]   = daily_loss
    app.bot_data["ex"]           = ex
    app.bot_data["kill_switch"]  = kill_switch

    # Register handlers
    app.add_handler(CommandHandler("status",  _status))
    app.add_handler(CommandHandler("pause",   _pause))
    app.add_handler(CommandHandler("resume",  _resume))
    app.add_handler(CommandHandler("close",   _close_all))
    app.add_handler(CommandHandler("daily",   _daily))
    app.add_handler(CommandHandler("equity",  _equity))
    app.add_handler(CommandHandler("options", _options))
    app.add_handler(CommandHandler("help",    _help))

    log.info("[BOT] Telegram bot polling started.")
    app.run_polling(stop_signals=None)   # stop_signals=None → no SIGINT conflict with main thread


# ─── PUBLIC ENTRY POINT ───────────────────────────────────────────────────────

def start(tracker, daily_trades, daily_loss, ex, kill_switch):
    """
    Called by live_trade.py main().
    Starts bot in background daemon thread — does not block.
    """
    env   = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")

    if not token:
        log.warning("[BOT] TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        return

    t = threading.Thread(
        target=_run_bot,
        args=(token, tracker, daily_trades, daily_loss, ex, kill_switch),
        name="TelegramBot",
        daemon=True,
    )
    t.start()
    log.info("[BOT] Telegram bot thread started.")