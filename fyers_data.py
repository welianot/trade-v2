"""
fyers_data.py
=============
Fetch market data from Fyers API.
Run fyers_auth.py first to generate access_token.

Features:
  - Live quotes (Nifty, BankNifty, Sensex)
  - Option chain (all strikes, OI, IV, Greeks)
  - Historical OHLCV data
"""

import json
from fyers_apiv3 import fyersModel

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

_ENV      = _load_env()
CLIENT_ID = _ENV.get("FYERS_CLIENT_ID", "")
TOKEN_FILE = "fyers_token.txt"

# ─── Init Fyers client ────────────────────────────────────────────────────────

def get_fyers():
    try:
        with open(TOKEN_FILE) as f:
            access_token = f.read().strip()
    except FileNotFoundError:
        print("ERROR: fyers_token.txt not found. Run fyers_auth.py first.")
        return None

    fyers = fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=access_token,
        log_path="",
    )
    return fyers

# ─── Live quotes ─────────────────────────────────────────────────────────────

def get_quotes(fyers, symbols: list):
    """
    Get live LTP for symbols.
    Symbol format: NSE:NIFTY50-INDEX, NSE:BANKNIFTY-INDEX, BSE:SENSEX-INDEX
    """
    data = {"symbols": ",".join(symbols)}
    response = fyers.quotes(data=data)
    if response.get("s") != "ok":
        print(f"Quotes error: {response}")
        return None
    return response["d"]

# ─── Option chain ─────────────────────────────────────────────────────────────

def get_option_chain(fyers, symbol: str, strike_count: int = 10):
    """
    Get option chain for index.
    symbol: NSE:NIFTY50-INDEX or NSE:BANKNIFTY-INDEX
    strike_count: number of strikes above/below ATM
    """
    data = {
        "symbol": symbol,
        "strikecount": strike_count,
        "timestamp": "",
    }
    response = fyers.optionchain(data=data)
    if response.get("s") != "ok":
        print(f"Option chain error: {response}")
        return None
    return response["data"]

# ─── Historical OHLCV ────────────────────────────────────────────────────────

def get_history(fyers, symbol: str, resolution: str = "15", days_back: int = 5):
    """
    Get historical candles.
    resolution: 1, 5, 15, 30, 60, D, W, M
    symbol: NSE:NIFTY50-INDEX
    """
    import time
    now = int(time.time())
    from_ts = now - (days_back * 24 * 3600)

    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "0",
        "range_from": str(from_ts),
        "range_to": str(now),
        "cont_flag": "1",
    }
    response = fyers.history(data=data)
    if response.get("s") != "ok":
        print(f"History error: {response}")
        return None
    return response["candles"]  # [[timestamp, O, H, L, C, V], ...]

# ─── Main (demo) ──────────────────────────────────────────────────────────────

def main():
    fyers = get_fyers()
    if not fyers:
        return

    print("=" * 50)
    print("  FYERS DATA TEST")
    print("=" * 50)

    # 1. Live quotes
    print("\n📊 Live Quotes:")
    quotes = get_quotes(fyers, [
        "NSE:NIFTY50-INDEX",
        "NSE:NIFTYBANK-INDEX",
        "BSE:SENSEX-INDEX",
    ])
    if quotes:
        for q in quotes:
            v = q.get("v", {})
            print(f"  {q['n']}: {v.get('lp', 'N/A')} | Chg: {v.get('ch', 'N/A')} ({v.get('chp', 'N/A')}%)")

    # 2. Nifty option chain
    print("\n📈 Nifty Option Chain (ATM ±5 strikes):")
    chain = get_option_chain(fyers, "NSE:NIFTY50-INDEX", strike_count=5)
    if chain:
        all_items = chain.get("optionsChain", [])

        # First item (strike_price=-1) is underlying info
        underlying_item = next((x for x in all_items if x.get("strike_price") == -1), {})
        underlying_ltp = underlying_item.get("ltp", "N/A")
        print(f"  Underlying LTP: {underlying_ltp}")

        # Expiry
        expiry_list = chain.get("expiryData", [])
        if expiry_list:
            print(f"  Nearest expiry timestamp: {expiry_list[0].get('expiry', 'N/A')}")

        # India VIX
        vix = chain.get("indiavixData", {})
        print(f"  India VIX: {vix.get('ltp', 'N/A')}")

        # Total OI
        print(f"  Total Call OI: {chain.get('callOi', 'N/A')}")
        print(f"  Total Put OI:  {chain.get('putOi', 'N/A')}")

        # Actual option strikes (skip underlying row)
        options = [x for x in all_items if x.get("strike_price", -1) > 0]
        calls = sorted([x for x in options if x.get("option_type") == "CE"],
                       key=lambda x: x["strike_price"])
        puts  = sorted([x for x in options if x.get("option_type") == "PE"],
                       key=lambda x: x["strike_price"])

        print(f"\n  {'Strike':>8} | {'CE LTP':>8} {'CE OI':>10} | {'PE LTP':>8} {'PE OI':>10}")
        print(f"  {'-'*8}-+-{'-'*8}-{'-'*10}-+-{'-'*8}-{'-'*10}")

        strikes = sorted(set(x["strike_price"] for x in options))
        call_map = {x["strike_price"]: x for x in calls}
        put_map  = {x["strike_price"]: x for x in puts}

        for strike in strikes:
            c = call_map.get(strike, {})
            p = put_map.get(strike, {})
            print(f"  {strike:>8} | {c.get('ltp','-'):>8} {c.get('oi','-'):>10} | {p.get('ltp','-'):>8} {p.get('oi','-'):>10}")

    # 3. Historical data
    print("\n📉 Nifty 15m candles (last 2 days):")
    candles = get_history(fyers, "NSE:NIFTY50-INDEX", resolution="15", days_back=2)
    if candles:
        print(f"  Total candles: {len(candles)}")
        last = candles[-1]
        print(f"  Last candle: O={last[1]} H={last[2]} L={last[3]} C={last[4]} V={last[5]}")

    print("\n✅ Done.")

if __name__ == "__main__":
    main()