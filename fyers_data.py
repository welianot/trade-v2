"""
fyers_data.py
=============
Fetch market data from Fyers API.
Run fyers_auth.py first to generate access_token.

Features:
  - Live quotes (Nifty, BankNifty, Sensex)
  - Option chain (all strikes, OI, IV, Greeks)
  - Historical OHLCV data
  - Response caching to avoid 429 rate limit errors
"""

import json
import time
import threading
from fyers_apiv3 import fyersModel

# ─── Global rate limiter ──────────────────────────────────────────────────────
_fyers_lock      = threading.Lock()
_last_call_time  = 0.0
MIN_CALL_GAP     = 12  # seconds between any two Fyers API calls

# ─── Cache ────────────────────────────────────────────────────────────────────
_cache      = {}          # {cache_key: (data, timestamp)}
_cache_lock = threading.Lock()

CACHE_TTL = {
    "quotes":  15,    # 15s — prices change fast
    "chain":   30,    # 30s — OI changes slower
    "history": 120,   # 2min — candles change every candle close
}

def _get_cache(key: str) -> object:
    with _cache_lock:
        if key in _cache:
            data, ts, ttl = _cache[key]
            if time.time() - ts < ttl:
                return data
    return None

def _set_cache(key: str, data: object, ttl: int):
    with _cache_lock:
        _cache[key] = (data, time.time(), ttl)

def clear_cache():
    """Force clear all cached data. Call when fresh data needed."""
    with _cache_lock:
        _cache.clear()

def cache_status() -> dict:
    """Return cache status — useful for debugging."""
    with _cache_lock:
        now = time.time()
        status = {}
        for k, (_, ts, ttl) in _cache.items():
            age     = round(now - ts, 1)
            expires = round(ttl - age, 1)
            status[k] = {"age_s": age, "expires_in_s": max(0, expires)}
    return status


# ─── Rate limited call ────────────────────────────────────────────────────────

def _rate_limited_call(fn, *args, **kwargs):
    """Wrap any Fyers API call with global rate limiter."""
    global _last_call_time
    with _fyers_lock:
        elapsed = time.time() - _last_call_time
        if elapsed < MIN_CALL_GAP:
            time.sleep(MIN_CALL_GAP - elapsed)
        _last_call_time = time.time()
    return fn(*args, **kwargs)


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

_ENV       = _load_env()
CLIENT_ID  = _ENV.get("FYERS_CLIENT_ID", "")
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

def get_quotes(fyers, symbols: list, force_refresh: bool = False):
    """
    Get live LTP for symbols.
    Symbol format: NSE:NIFTY50-INDEX, NSE:NIFTYBANK-INDEX, BSE:SENSEX-INDEX
    Cached for 15 seconds.
    """
    cache_key = f"quotes_{'_'.join(sorted(symbols))}"

    if not force_refresh:
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached

    data     = {"symbols": ",".join(symbols)}
    response = _rate_limited_call(fyers.quotes, data=data)

    if response.get("s") != "ok":
        print(f"Quotes error: {response}")
        return None

    result = response["d"]
    _set_cache(cache_key, result, CACHE_TTL["quotes"])
    return result


# ─── Option chain ─────────────────────────────────────────────────────────────

def get_option_chain(fyers, symbol: str, strike_count: int = 10, force_refresh: bool = False):
    """
    Get option chain for index.
    symbol: NSE:NIFTY50-INDEX or NSE:BANKNIFTY-INDEX
    strike_count: number of strikes above/below ATM
    Cached for 30 seconds.
    """
    cache_key = f"chain_{symbol}_{strike_count}"

    if not force_refresh:
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached

    data = {
        "symbol":      symbol,
        "strikecount": strike_count,
        "timestamp":   "",
    }
    response = _rate_limited_call(fyers.optionchain, data=data)

    if response.get("s") != "ok":
        print(f"Option chain error: {response}")
        return None

    result = response["data"]
    _set_cache(cache_key, result, CACHE_TTL["chain"])
    return result


# ─── Historical OHLCV ────────────────────────────────────────────────────────

def get_history(fyers, symbol: str, resolution: str = "15", days_back: int = 5, force_refresh: bool = False):
    """
    Get historical candles.
    resolution: 1, 5, 15, 30, 60, D, W, M
    symbol: NSE:NIFTY50-INDEX
    Cached for 2 minutes.
    """
    cache_key = f"history_{symbol}_{resolution}_{days_back}"

    if not force_refresh:
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached

    now     = int(time.time())
    from_ts = now - (days_back * 24 * 3600)

    data = {
        "symbol":      symbol,
        "resolution":  resolution,
        "date_format": "0",
        "range_from":  str(from_ts),
        "range_to":    str(now),
        "cont_flag":   "1",
    }
    response = _rate_limited_call(fyers.history, data=data)

    if response.get("s") != "ok":
        print(f"History error: {response}")
        return None

    result = response["candles"]
    _set_cache(cache_key, result, CACHE_TTL["history"])
    return result


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

    # 2. Cache test — second call should be instant
    print("\n🔄 Cache test (second quotes call — should be instant):")
    t0     = time.time()
    quotes = get_quotes(fyers, ["NSE:NIFTY50-INDEX"])
    print(f"  Returned in {round(time.time()-t0, 3)}s (cached)")

    # 3. Cache status
    print("\n📦 Cache status:")
    for k, v in cache_status().items():
        print(f"  {k}: age={v['age_s']}s expires_in={v['expires_in_s']}s")

    # 4. Nifty option chain
    print("\n📈 Nifty Option Chain (ATM ±5 strikes):")
    chain = get_option_chain(fyers, "NSE:NIFTY50-INDEX", strike_count=5)
    if chain:
        all_items = chain.get("optionsChain", [])

        underlying_item = next((x for x in all_items if x.get("strike_price") == -1), {})
        underlying_ltp  = underlying_item.get("ltp", "N/A")
        print(f"  Underlying LTP: {underlying_ltp}")

        expiry_list = chain.get("expiryData", [])
        if expiry_list:
            print(f"  Nearest expiry timestamp: {expiry_list[0].get('expiry', 'N/A')}")

        vix = chain.get("indiavixData", {})
        print(f"  India VIX: {vix.get('ltp', 'N/A')}")
        print(f"  Total Call OI: {chain.get('callOi', 'N/A')}")
        print(f"  Total Put OI:  {chain.get('putOi', 'N/A')}")

        options = [x for x in all_items if x.get("strike_price", -1) > 0]
        calls   = sorted([x for x in options if x.get("option_type") == "CE"], key=lambda x: x["strike_price"])
        puts    = sorted([x for x in options if x.get("option_type") == "PE"], key=lambda x: x["strike_price"])

        print(f"\n  {'Strike':>8} | {'CE LTP':>8} {'CE OI':>10} | {'PE LTP':>8} {'PE OI':>10}")
        print(f"  {'-'*8}-+-{'-'*8}-{'-'*10}-+-{'-'*8}-{'-'*10}")

        strikes  = sorted(set(x["strike_price"] for x in options))
        call_map = {x["strike_price"]: x for x in calls}
        put_map  = {x["strike_price"]: x for x in puts}

        for strike in strikes:
            c = call_map.get(strike, {})
            p = put_map.get(strike, {})
            print(f"  {strike:>8} | {c.get('ltp','-'):>8} {c.get('oi','-'):>10} | {p.get('ltp','-'):>8} {p.get('oi','-'):>10}")

    # 5. Historical data
    print("\n📉 Nifty 15m candles (last 2 days):")
    candles = get_history(fyers, "NSE:NIFTY50-INDEX", resolution="15", days_back=2)
    if candles:
        print(f"  Total candles: {len(candles)}")
        last = candles[-1]
        print(f"  Last candle: O={last[1]} H={last[2]} L={last[3]} C={last[4]} V={last[5]}")

    print("\n✅ Done.")


if __name__ == "__main__":
    main()