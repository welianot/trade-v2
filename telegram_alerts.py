# telegram_alerts.py
# Shared module — send alerts to Telegram.
# Used by live_trade.py and bot_server.py.

import os
import requests

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

_ENV      = _load_env()
BOT_TOKEN = _ENV.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = _ENV.get("TELEGRAM_CHAT_ID", "")

def send(msg: str):
    """Fire-and-forget Telegram message. Silently fails if not configured."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        print(f"[TG] send failed: {e}")