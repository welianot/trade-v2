"""
fyers_data.py
=============
Fetch market data from Fyers API.
Run fyers_auth.py once to generate tokens.
Auto-refreshes access token daily using refresh token (valid 30 days).

Features:
  - Auto token refresh (no daily re-auth needed)
  - Live quotes (Nifty, BankNifty, Sensex)
  - Option chain (all strikes, OI, IV, Greeks)
  - Historical OHLCV data
  - Response caching to avoid 429 rate limit errors
  - Timeout protection on all API calls
"""

import json
import time
import threading
from datetime import date
from fyers_apiv3 import fyersModel

# ─── Global rate limiter ─────────────────────────────────────────────────────
_fyers_lock     = threading.Lock()
_last_call_time = 0.0
MIN_CALL_GAP    = 3  # seconds between any two Fyers API calls

# ─── Cache ───────────────────────────────────────────────────────────────────
_cache      = {}
_cache_lock = threading.Lock()

CACHE_TTL = {
    "quotes":  15,
    "chain":   30,
    "history": 120,
}

def _get_cache(key: str):
    with _cache_lock:
        if key in _cache:
            data, ts, ttl = _cache[key]
            if time.time() - ts < ttl:
                return data
    return None

def _set_cache(key: str, data, ttl: int):
    with _cache_lock:
        _cache[key] = (data, time.time(), ttl)

def clear_cache():
    with _cache_lock:
        _cache.clear()

def cache_status() -> dict:
    with _cache_lock:
        now    = time.time()
        status = {}
        for k, (_, ts, ttl) in _cache.items():
            age     = round(now - ts, 1)
            expires = round(ttl - age, 1)
            status[k] = {"age_s": age, "expires_in_s": max(0, expires)}
    return status


# ─── Timeout wrapper ─────────────────────────────────────────────────────────

def _call_with_timeout(fn, args=(), kwargs={}, timeout=15):
    result = [None]
    error  = [None]

    def target():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        return None
    if error[0]:
        raise error[0]
    return result[0]


# ─── Rate limited call ───────────────────────────────────────────────────────

def _rate_limited_call(fn, *args, **kwargs):
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


# ─── Auto-refresh helper ─────────────────────────────────────────────────────

def _refresh_access_token(data: dict) -> dict:
    """Use refresh_token to get new access_token. Updates data dict + saves file."""
    try:
        session = fyersModel.SessionModel(
            client_id=data["client_id"],
            secret_key=data["secret_key"],
            redirect_uri=data.get("redirect_uri", "http://127.0.0.1:5000/"),
            response_type="code",
            grant_type="refresh_token",
        )
        session.set_token(data["refresh_token"])
        resp = session.generate_token()

        if resp.get("s") == "ok":
            data["access_token"] = resp["access_token"]
            data["saved_date"]   = str(date.today())
            # Save updated token
            with open(TOKEN_FILE, "w") as f:
                json.dump(data, f, indent=2)
            print(f"[Fyers] Access token auto-refreshed ({date.today()})")
            return data
        else:
            print(f"[Fyers] Auto-refresh failed: {resp} — using existing token")
            return data

    except Exception as e:
        print(f"[Fyers] Auto-refresh error: {e} — using existing token")
        return data


# ─── Init Fyers client ───────────────────────────────────────────────────────

def get_fyers():
    """
    Load token, auto-refresh if expired (new day), return FyersModel.
    Falls back gracefully if refresh fails.
    """
    try:
        with open(TOKEN_FILE) as f:
            raw = f.read().strip()
    except FileNotFoundError:
        print("ERROR: fyers_token.txt not found. Run fyers_auth.py first.")
        return None

    # Handle old format (plain token string) vs new format (JSON)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Old plain-string format — wrap it, no refresh token available
        print("[Fyers] Old token format detected. Run fyers_auth.py to enable auto-refresh.")
        data = {
            "access_token":  raw,
            "refresh_token": "",
            "client_id":     CLIENT_ID,
            "secret_key":    _ENV.get("FYERS_SECRET_KEY", ""),
            "saved_date":    "",
        }

    # Auto-refresh if token from previous day and refresh_token available
    if data.get("saved_date") != str(date.today()) and data.get("refresh_token"):
        data = _refresh_access_token(data)

    return fyersModel.FyersModel(
        client_id=data.get("client_id", CLIENT_ID),
        token=data["access_token"],
        log_path="",
    )


# ─── Live quotes ─────────────────────────────────────────────────────────────

def get_quotes(fyers, symbols: list, force_refresh: bool = False):
    """
    Get live LTP for symbols.
    Symbol format: NSE:NIFTY50-INDEX, NSE:NIFTYBANK-INDEX, BSE:SENSEX-INDEX
    Cached 15s. Timeout 25s.
    """
    cache_key = f"quotes_{'_'.join(sorted(symbols))}"

    if not force_refresh:
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached

    response = _call_with_timeout(
        _rate_limited_call,
        args=(fyers.quotes,),
        kwargs={"data": {"symbols": ",".join(symbols)}},
        timeout=25,
    )

    if response is None:
        print(f"Quotes fetch timed out: {symbols}")
        return None

    if response.get("s") != "ok":
        print(f"Quotes error: {response}")
        return None

    result = response["d"]
    _set_cache(cache_key, result, CACHE_TTL["quotes"])
    return result


# ─── Option chain ────────────────────────────────────────────────────────────

def get_option_chain(fyers, symbol: str, strike_count: int = 10, force_refresh: bool = False):
    """
    Get option chain for index.
    symbol: NSE:NIFTY50-INDEX or NSE:BANKNIFTY-INDEX
    Cached 30s. Timeout 25s.
    """
    cache_key = f"chain_{symbol}_{strike_count}"

    if not force_refresh:
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached

    response = _call_with_timeout(
        _rate_limited_call,
        args=(fyers.optionchain,),
        kwargs={"data": {"symbol": symbol, "strikecount": strike_count, "timestamp": ""}},
        timeout=25,
    )

    if response is None:
        print(f"Option chain fetch timed out: {symbol}")
        return None

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
    Cached 2min. Timeout 25s.
    """
    cache_key = f"history_{symbol}_{resolution}_{days_back}"

    if not force_refresh:
        cached = _get_cache(cache_key)
        if cached is not None:
            return cached

    now     = int(time.time())
    from_ts = now - (days_back * 24 * 3600)

    response = _call_with_timeout(
        _rate_limited_call,
        args=(fyers.history,),
        kwargs={"data": {
            "symbol":      symbol,
            "resolution":  resolution,
            "date_format": "0",
            "range_from":  str(from_ts),
            "range_to":    str(now),
            "cont_flag":   "1",
        }},
        timeout=25,
    )

    if response is None:
        print(f"History fetch timed out: {symbol}")
        return None

    if response.get("s") != "ok":
        print(f"History error: {response}")
        return None

    result = response["candles"]
    _set_cache(cache_key, result, CACHE_TTL["history"])
    return result


# ─── Main (demo) ─────────────────────────────────────────────────────────────

def main():
    fyers = get_fyers()
    if not fyers:
        return

    print("=" * 50)
    print("  FYERS DATA TEST")
    print("=" * 50)

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
    else:
        print("  ❌ Quotes fetch failed.")

    print("\n🔄 Cache test:")
    t0 = time.time()
    get_quotes(fyers, ["NSE:NIFTY50-INDEX"])
    print(f"  Returned in {round(time.time()-t0, 3)}s (cached)")

    print("\n📦 Cache status:")
    for k, v in cache_status().items():
        print(f"  {k}: age={v['age_s']}s expires_in={v['expires_in_s']}s")

    print("\n✅ Done.")


if __name__ == "__main__":
    main()