"""
=============================================================
STRATEGY: 4H Liquidity Grab + 15M SMC Entry
EXCHANGE: Delta Exchange (India)
SYMBOLS:  BTCUSDT, ETHUSDT, SOLUSDT
=============================================================

HOW IT WORKS:
  Phase 1 — 4H: Detect liquidity grabs (sweep of swing high/low with rejection)
  Phase 2 — 15M: Wait for BOS + FVG/OB pullback → enter

INSTALL:
  pip install ccxt pandas numpy matplotlib

RUN:
  python delta_smc_backtest.py
=============================================================
"""

import ccxt
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

# ─── CONFIG ────────────────────────────────────────────────────────────────────

# Delta Exchange symbol aliases
SYMBOL_ALIASES = {
    "BTCUSDT":  ["BTCUSDT", "BTC/USDT:USDT"],
    "ETHUSDT":  ["ETHUSDT", "ETH/USDT:USDT"],
}

SYMBOLS = list(SYMBOL_ALIASES.keys())

LOOKBACK_DAYS       = 180      # 6 months
RISK_PER_TRADE      = 0.01     # 1% account risk
ACCOUNT_SIZE        = 1000     # USDT start capital
MIN_RR              = 3.0      # minimum reward:risk
MAX_TRADES_PER_DAY  = 2
DAILY_LOSS_LIMIT    = 0.03     # 3% of equity

SWING_LOOKBACK      = 3        # candles each side for swing detection
MIN_WICK_PCT        = 0.003    # min wick size for grab validity
ENTRY_WINDOW        = 16       # 15M candles to look for BOS after grab (= 4H)

# Per-symbol tuning — tighter filters for noisier assets
SYMBOL_CONFIG = {
    "BTCUSDT": {
        "min_wick_pct":    0.004,   # 0.4% wick minimum
        "max_risk_pct":    0.013,   # max 1.3% SL distance
        "session_hours":   list(range(6, 23)),  # UTC 6-23 (London open to NY close)
        "slope_long":     -0.1,     # slight downslope ok for longs (recoveries)
        "slope_short":    -0.1,     # any slope ok for shorts
    },
    "ETHUSDT": {
        "min_wick_pct":    0.003,
        "max_risk_pct":    0.02,
        "session_hours":   list(range(0, 24)),  # all sessions (ETH works fine)
        "slope_long":     -0.5,
        "slope_short":    -0.5,
    },
    "SOLUSDT": {
        "min_wick_pct":    0.004,
        "max_risk_pct":    0.015,
        "session_hours":   list(range(0, 24)),
        "slope_long":     -0.5,
        "slope_short":    -0.5,
    },
}

# ─── EXCHANGE SETUP ────────────────────────────────────────────────────────────

def get_exchange():
    return ccxt.delta({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })


def fetch_ohlcv(exchange, symbol_key, timeframe, days):
    """Try multiple symbol aliases until one works."""
    since = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    aliases = SYMBOL_ALIASES.get(symbol_key, [symbol_key])

    for sym in aliases:
        all_candles = []
        limit = 500
        print(f"  Fetching {sym} {timeframe}...", end="", flush=True)
        try:
            _since = since
            while True:
                candles = exchange.fetch_ohlcv(sym, timeframe, since=_since, limit=limit)
                if not candles:
                    break
                all_candles.extend(candles)
                if len(candles) < limit:
                    break
                _since = candles[-1][0] + 1

            if not all_candles:
                print(" 0 candles, trying next alias...")
                continue

            df = pd.DataFrame(all_candles, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df = df.set_index("timestamp").sort_index()
            df = df[~df.index.duplicated(keep="first")]
            print(f" {len(df)} candles ✓  [{sym}]")
            return df, sym

        except Exception as e:
            print(f" failed ({e}), trying next...")

    print(f"  All aliases failed for {symbol_key}")
    return None, None


# ─── INDICATORS ────────────────────────────────────────────────────────────────

def add_emas(df):
    """50 EMA trend filter, 200 EMA macro bias, slope for trend strength."""
    df["ema50"]       = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"]      = df["close"].ewm(span=200, adjust=False).mean()
    df["ema50_slope"] = df["ema50"].pct_change(3) * 100
    return df


def detect_swing_highs(df, lookback=3):
    highs = df["high"].values
    result = np.zeros(len(highs), dtype=bool)
    for i in range(lookback, len(highs) - lookback):
        window = highs[i - lookback: i + lookback + 1]
        if highs[i] == window.max() and list(window).count(highs[i]) == 1:
            result[i] = True
    return result


def detect_swing_lows(df, lookback=3):
    lows = df["low"].values
    result = np.zeros(len(lows), dtype=bool)
    for i in range(lookback, len(lows) - lookback):
        window = lows[i - lookback: i + lookback + 1]
        if lows[i] == window.min() and list(window).count(lows[i]) == 1:
            result[i] = True
    return result


def detect_liquidity_grabs(df_4h, min_wick_pct=MIN_WICK_PCT, swing_lb=SWING_LOOKBACK):
    """
    Bearish grab: sweep above swing high, candle closes below → short bias
    Bullish grab: sweep below swing low, candle closes above → long bias
    """
    df = df_4h.copy()
    df["swing_high"] = detect_swing_highs(df, swing_lb)
    df["swing_low"]  = detect_swing_lows(df, swing_lb)

    grabs = []
    highs      = df["high"].values
    lows       = df["low"].values
    closes     = df["close"].values
    opens      = df["open"].values
    timestamps = df.index

    last_swing_highs = []
    last_swing_lows  = []

    for i in range(swing_lb * 2, len(df)):
        if df["swing_high"].iloc[i - swing_lb]:
            last_swing_highs.append((timestamps[i - swing_lb], highs[i - swing_lb]))
            if len(last_swing_highs) > 5:
                last_swing_highs.pop(0)

        if df["swing_low"].iloc[i - swing_lb]:
            last_swing_lows.append((timestamps[i - swing_lb], lows[i - swing_lb]))
            if len(last_swing_lows) > 5:
                last_swing_lows.pop(0)

        # BEARISH GRAB
        for sh_time, sh_level in last_swing_highs:
            if sh_time >= timestamps[i]:
                continue
            upper_wick = highs[i] - max(opens[i], closes[i])
            wick_pct   = upper_wick / sh_level
            if highs[i] > sh_level and closes[i] < sh_level and wick_pct >= min_wick_pct:
                grabs.append({
                    "grab_time": timestamps[i], "grab_type": "bearish",
                    "grab_level": sh_level, "wick_pct": wick_pct,
                    "close": closes[i], "open": opens[i],
                    "high": highs[i], "low": lows[i],
                })
                break

        # BULLISH GRAB
        for sl_time, sl_level in last_swing_lows:
            if sl_time >= timestamps[i]:
                continue
            lower_wick = min(opens[i], closes[i]) - lows[i]
            wick_pct   = lower_wick / sl_level
            if lows[i] < sl_level and closes[i] > sl_level and wick_pct >= min_wick_pct:
                grabs.append({
                    "grab_time": timestamps[i], "grab_type": "bullish",
                    "grab_level": sl_level, "wick_pct": wick_pct,
                    "close": closes[i], "open": opens[i],
                    "high": highs[i], "low": lows[i],
                })
                break

    return pd.DataFrame(grabs)


def detect_fvg(df_15m, idx):
    """Fair Value Gap: 3-candle pattern."""
    if idx < 2:
        return None
    c0 = df_15m.iloc[idx - 2]
    c2 = df_15m.iloc[idx]
    if c0["high"] < c2["low"]:
        return ("bullish", c2["low"], c0["high"])
    if c0["low"] > c2["high"]:
        return ("bearish", c0["low"], c2["high"])
    return None


def detect_bos(df_15m, start_idx, direction, window=8):
    """Break of Structure on 15M."""
    subset = df_15m.iloc[start_idx: start_idx + window]
    if len(subset) < 3:
        return None
    if direction == "long":
        ref_high = subset["high"].iloc[:3].max()
        for i in range(3, len(subset)):
            if subset["close"].iloc[i] > ref_high:
                return start_idx + i
    else:
        ref_low = subset["low"].iloc[:3].min()
        for i in range(3, len(subset)):
            if subset["close"].iloc[i] < ref_low:
                return start_idx + i
    return None


# ─── BACKTEST ENGINE ───────────────────────────────────────────────────────────

def run_backtest(symbol, df_4h, df_15m, sym_cfg=None):
    if sym_cfg is None:
        sym_cfg = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG['ETHUSDT'])
    print(f"\n{'='*60}")
    print(f"  BACKTEST: {symbol}")
    print(f"{'='*60}")

    df_4h  = add_emas(df_4h.copy())
    grabs  = detect_liquidity_grabs(df_4h)

    if grabs.empty:
        print("  No grabs detected.")
        return None

    print(f"  4H Grabs detected: {len(grabs)}")

    trades       = []
    equity       = ACCOUNT_SIZE
    equity_curve = [{"time": df_15m.index[0], "equity": equity}]
    daily_trades = {}
    daily_loss   = {}

    for _, grab in grabs.iterrows():
        grab_time = grab["grab_time"]
        grab_type = grab["grab_type"]
        direction = "long" if grab_type == "bullish" else "short"

        # ── SYMBOL-AWARE FILTERS ──────────────────────────────────
        # 1. Wick size: grab must have meaningful rejection
        if grab["wick_pct"] < sym_cfg["min_wick_pct"]:
            continue

        # 2. Session filter: skip low-liquidity hours (UTC)
        if grab_time.hour not in sym_cfg["session_hours"]:
            continue

        # 3. Trend filter via ema50 slope
        try:
            slope_at_grab  = df_4h.loc[df_4h.index <= grab_time, "ema50_slope"].iloc[-1]
            ema200_at_grab = df_4h.loc[df_4h.index <= grab_time, "ema200"].iloc[-1]
        except:
            continue
        if direction == "long"  and grab["close"] < ema200_at_grab and slope_at_grab < sym_cfg["slope_long"]:
            continue
        if direction == "short" and grab["close"] > ema200_at_grab and slope_at_grab > abs(sym_cfg["slope_short"]):
            continue

        # Daily limits
        day_key = grab_time.date()
        if daily_trades.get(day_key, 0) >= MAX_TRADES_PER_DAY:    continue
        if daily_loss.get(day_key, 0)   >= DAILY_LOSS_LIMIT * equity: continue

        try:
            m15_start_idx = df_15m.index.searchsorted(grab_time)
        except:
            continue
        if m15_start_idx >= len(df_15m) - ENTRY_WINDOW:
            continue

        bos_idx = detect_bos(df_15m, m15_start_idx, direction, window=ENTRY_WINDOW)
        if bos_idx is None:
            continue

        # FVG hunt after BOS
        entry_price = sl_price = tp_price = None
        entry_idx   = None

        for j in range(bos_idx + 1, min(bos_idx + 8, len(df_15m) - 1)):
            fvg = detect_fvg(df_15m, j)
            if fvg is None:
                continue
            fvg_type, fvg_top, fvg_bottom = fvg
            fvg_mid = (fvg_top + fvg_bottom) / 2

            if direction == "long" and fvg_type == "bullish":
                entry_price = fvg_mid
                sl_price    = grab["low"] * (1 - 0.001)
                risk        = entry_price - sl_price
                if risk <= 0: continue
                if risk / entry_price > sym_cfg["max_risk_pct"]: continue  # skip wide SL
                tp_price  = entry_price + risk * MIN_RR
                entry_idx = j
                break

            elif direction == "short" and fvg_type == "bearish":
                entry_price = fvg_mid
                sl_price    = grab["high"] * (1 + 0.001)
                risk        = sl_price - entry_price
                if risk <= 0: continue
                if risk / entry_price > sym_cfg["max_risk_pct"]: continue  # skip wide SL
                tp_price  = entry_price - risk * MIN_RR
                entry_idx = j
                break

        if entry_price is None or entry_idx is None:
            continue

        # Walk forward: find SL or TP hit
        trade_result = exit_price = exit_time = None

        for k in range(entry_idx + 1, min(entry_idx + 50, len(df_15m))):
            c = df_15m.iloc[k]
            if direction == "long":
                if c["low"]  <= sl_price: trade_result = "loss"; exit_price = sl_price; exit_time = df_15m.index[k]; break
                if c["high"] >= tp_price: trade_result = "win";  exit_price = tp_price; exit_time = df_15m.index[k]; break
            else:
                if c["high"] >= sl_price: trade_result = "loss"; exit_price = sl_price; exit_time = df_15m.index[k]; break
                if c["low"]  <= tp_price: trade_result = "win";  exit_price = tp_price; exit_time = df_15m.index[k]; break

        if trade_result is None:
            continue

        # PnL
        risk_amount = equity * RISK_PER_TRADE
        if direction == "long":
            size = risk_amount / (entry_price - sl_price)
            pnl  = (exit_price - entry_price) * size
        else:
            size = risk_amount / (sl_price - entry_price)
            pnl  = (entry_price - exit_price) * size

        equity += pnl
        daily_trades[day_key] = daily_trades.get(day_key, 0) + 1
        if pnl < 0:
            daily_loss[day_key] = daily_loss.get(day_key, 0) + abs(pnl)

        trades.append({
            "symbol":       symbol,
            "direction":    direction,
            "grab_time":    grab_time,
            "entry_time":   df_15m.index[entry_idx],
            "exit_time":    exit_time,
            "entry_price":  round(entry_price, 4),
            "sl_price":     round(sl_price, 4),
            "tp_price":     round(tp_price, 4),
            "exit_price":   round(exit_price, 4),
            "result":       trade_result,
            "pnl_usdt":     round(pnl, 2),
            "equity_after": round(equity, 2),
            "rr_achieved":  round(abs(exit_price - entry_price) / abs(entry_price - sl_price), 2),
        })
        equity_curve.append({"time": exit_time, "equity": equity})

    return trades, equity_curve


# ─── REPORTING ─────────────────────────────────────────────────────────────────

def print_stats(symbol, trades):
    if not trades:
        print(f"  {symbol}: No trades.")
        return
    df      = pd.DataFrame(trades)
    wins    = df[df["result"] == "win"]
    losses  = df[df["result"] == "loss"]
    total   = len(df)
    winrate = len(wins) / total * 100

    total_pnl     = df["pnl_usdt"].sum()
    avg_win       = wins["pnl_usdt"].mean()   if len(wins)   > 0 else 0
    avg_loss      = losses["pnl_usdt"].mean() if len(losses) > 0 else 0
    pf_denom      = abs(losses["pnl_usdt"].sum())
    profit_factor = wins["pnl_usdt"].sum() / pf_denom if pf_denom > 0 else 0

    peak = ACCOUNT_SIZE; max_dd = 0
    for e in df["equity_after"].values:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd: max_dd = dd

    print(f"\n  ── {symbol} Results ──")
    print(f"  Total trades:    {total}")
    print(f"  Wins / Losses:   {len(wins)} / {len(losses)}")
    print(f"  Win Rate:        {winrate:.1f}%")
    print(f"  Total PnL:       ${total_pnl:.2f}")
    print(f"  Avg Win:         ${avg_win:.2f}")
    print(f"  Avg Loss:        ${avg_loss:.2f}")
    print(f"  Profit Factor:   {profit_factor:.2f}")
    print(f"  Avg RR:          {df['rr_achieved'].mean():.2f}")
    print(f"  Max Drawdown:    {max_dd:.1f}%")
    print(f"  Final Equity:    ${df['equity_after'].iloc[-1]:.2f}")

    longs  = df[df["direction"] == "long"]
    shorts = df[df["direction"] == "short"]
    if len(longs)  > 0:
        print(f"  Long WR:         {len(longs[longs['result']=='win'])/len(longs)*100:.1f}% ({len(longs)} trades)")
    if len(shorts) > 0:
        print(f"  Short WR:        {len(shorts[shorts['result']=='win'])/len(shorts)*100:.1f}% ({len(shorts)} trades)")


def _scalar(val):
    """Safely extract scalar from pandas Series or scalar."""
    if isinstance(val, pd.Series):
        if len(val) == 0:
            return None
        return float(val.iloc[-1])
    return float(val)


def plot_results(all_results):
    n = len(all_results)
    fig, axes = plt.subplots(n, 1, figsize=(14, 5 * n))
    if n == 1:
        axes = [axes]

    fig.suptitle("4H Liquidity Grab + 15M SMC Entry — Delta Exchange Backtest",
                 fontsize=14, fontweight="bold", color="#eee")

    for ax, (symbol, trades, equity_curve) in zip(axes, all_results):
        if not trades:
            ax.set_title(f"{symbol} — No trades")
            continue

        df_t  = pd.DataFrame(trades)
        eq_df = pd.DataFrame(equity_curve)
        eq_df = eq_df.dropna(subset=["time"]).set_index("time").sort_index()
        # deduplicate index
        eq_df = eq_df[~eq_df.index.duplicated(keep="last")]

        ax.plot(eq_df.index, eq_df["equity"], color="#00d4aa", linewidth=2, label="Equity")
        ax.axhline(y=ACCOUNT_SIZE, color="#555", linestyle="--", linewidth=1, label="Start")

        # Scatter dots — safe scalar extraction
        for _, row in df_t.iterrows():
            t = row["exit_time"]
            if pd.isnull(t) or t not in eq_df.index:
                continue
            eq_val = _scalar(eq_df.loc[t, "equity"])
            if eq_val is None:
                continue
            color = "#00ff88" if row["result"] == "win" else "#ff4444"
            ax.scatter(t, eq_val, color=color, s=30, zorder=5)

        wins    = df_t[df_t["result"] == "win"]
        total_pnl = df_t["pnl_usdt"].sum()
        winrate   = len(wins) / len(df_t) * 100
        final_eq  = df_t["equity_after"].iloc[-1]

        ax.set_title(
            f"{symbol}  |  Trades: {len(df_t)}  |  WR: {winrate:.1f}%  "
            f"|  PnL: ${total_pnl:.2f}  |  Final: ${final_eq:.2f}",
            fontsize=11, color="#eee"
        )
        ax.set_ylabel("Equity (USDT)", color="#aaa")
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#aaa")
        for sp in ["top","right"]: ax.spines[sp].set_visible(False)
        for sp in ["bottom","left"]: ax.spines[sp].set_color("#333")
        ax.legend(loc="upper left", facecolor="#1a1a2e", edgecolor="#333", labelcolor="#ccc")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.grid(axis="y", color="#1e1e2e", linewidth=0.5)

    fig.patch.set_facecolor("#0d1117")
    plt.tight_layout()
    plt.savefig("backtest_results.png", dpi=150, bbox_inches="tight", facecolor="#0d1117")
    print("\n  Chart saved: backtest_results.png")
    plt.show()


def export_trades(all_results):
    all_trades = [t for _, trades, _ in all_results if trades for t in trades]
    if all_trades:
        pd.DataFrame(all_trades).to_csv("backtest_trades.csv", index=False)
        print("  Trades exported: backtest_trades.csv")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*60)
    print("  4H LIQUIDITY GRAB + 15M SMC ENTRY BACKTEST")
    print("  Exchange: Delta Exchange | BTC / ETH")
    print("="*60)

    exchange    = get_exchange()
    all_results = []

    print("\nFetching OHLCV data...")

    for symbol_key in SYMBOLS:
        print(f"\n[{symbol_key}]")
        df_4h, sym_4h = fetch_ohlcv(exchange, symbol_key, "4h",  LOOKBACK_DAYS)
        df_15m, _     = fetch_ohlcv(exchange, symbol_key, "15m", LOOKBACK_DAYS)

        if df_4h is None or df_15m is None:
            print(f"  Skipping {symbol_key} — fetch failed")
            continue
        if len(df_4h) < 50 or len(df_15m) < 200:
            print(f"  Skipping {symbol_key} — insufficient data ({len(df_4h)} 4H, {len(df_15m)} 15M candles)")
            continue

        cfg = SYMBOL_CONFIG.get(symbol_key, SYMBOL_CONFIG['ETHUSDT'])
        result = run_backtest(symbol_key, df_4h, df_15m, sym_cfg=cfg)
        if result is None:
            continue

        trades, equity_curve = result
        print_stats(symbol_key, trades)
        all_results.append((symbol_key, trades, equity_curve))

    if all_results:
        print("\n" + "="*60)
        print("  Generating charts...")
        plot_results(all_results)
        export_trades(all_results)
        print("\n  Done. Check backtest_results.png and backtest_trades.csv")
    else:
        print("\n  No results to display.")


if __name__ == "__main__":
    main()