"""
=============================================================
STRATEGY: 4H Liquidity Grab + 15M SMC Entry
EXCHANGE: Delta Exchange (India)
SYMBOLS:  BTCUSDT, ETHUSDT
VALIDATION: Walk-Forward + Monte Carlo
=============================================================

HOW IT WORKS:
  Phase 1 — 4H: Detect liquidity grabs (sweep of swing high/low with rejection)
  Phase 2 — 15M: Wait for BOS + FVG/OB pullback → enter
  Phase 3 — Validate: Walk-forward OOS test + Monte Carlo confidence

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

SYMBOL_ALIASES = {
    "BTCUSDT": ["BTCUSDT", "BTC/USDT:USDT"],
    "ETHUSDT": ["ETHUSDT", "ETH/USDT:USDT"],
}

SYMBOLS = list(SYMBOL_ALIASES.keys())

LOOKBACK_DAYS      = 365
RISK_PER_TRADE     = 0.01
ACCOUNT_SIZE       = 1000
MIN_RR             = 3.0
MAX_TRADES_PER_DAY = 10
DAILY_LOSS_LIMIT   = 0.03

SWING_LOOKBACK = 3
MIN_WICK_PCT   = 0.003
ENTRY_WINDOW   = 16

# Walk-forward config
WF_TRAIN_PCT  = 0.70   # 70% train, 30% OOS
WF_WINDOWS    = 2      # 2 windows = bigger slices, more trades per OOS

# Monte Carlo config
MC_RUNS       = 1000
MC_CONFIDENCE = 0.95

SYMBOL_CONFIG = {
    "BTCUSDT": {
        "min_wick_pct":  0.003,
        "max_risk_pct":  0.025,
        "session_hours": list(range(6, 23)),
        "slope_long":   -1,
        "slope_short":  -1,
        "entry_window":  32,
    },
    "ETHUSDT": {
        "min_wick_pct":  0.003,
        "max_risk_pct":  0.02,
        "session_hours": list(range(0, 24)),
        "slope_long":   -1,
        "slope_short":  -1,
        "entry_window":  24,
    },
}

# ─── EXCHANGE ──────────────────────────────────────────────────────────────────

def get_exchange():
    return ccxt.delta({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })


def fetch_ohlcv(exchange, symbol_key, timeframe, days):
    since   = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
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
    df["ema50"]       = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"]      = df["close"].ewm(span=200, adjust=False).mean()
    df["ema50_slope"] = df["ema50"].pct_change(3) * 100
    return df


def detect_swing_highs(df, lookback=3):
    highs  = df["high"].values
    result = np.zeros(len(highs), dtype=bool)
    for i in range(lookback, len(highs) - lookback):
        window = highs[i - lookback: i + lookback + 1]
        if highs[i] == window.max() and list(window).count(highs[i]) == 1:
            result[i] = True
    return result


def detect_swing_lows(df, lookback=3):
    lows   = df["low"].values
    result = np.zeros(len(lows), dtype=bool)
    for i in range(lookback, len(lows) - lookback):
        window = lows[i - lookback: i + lookback + 1]
        if lows[i] == window.min() and list(window).count(lows[i]) == 1:
            result[i] = True
    return result


def detect_liquidity_grabs(df_4h, min_wick_pct=MIN_WICK_PCT, swing_lb=SWING_LOOKBACK):
    df = df_4h.copy()
    df["swing_high"] = detect_swing_highs(df, swing_lb)
    df["swing_low"]  = detect_swing_lows(df, swing_lb)

    grabs      = []
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


# ─── CORE BACKTEST ─────────────────────────────────────────────────────────────

def run_backtest(symbol, df_4h, df_15m, sym_cfg=None, date_range=None):
    """
    Run backtest on given data slice.
    date_range: (start, end) pd.Timestamp tuple for walk-forward slicing.
    """
    if sym_cfg is None:
        sym_cfg = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["ETHUSDT"])

    # Slice data for walk-forward window
    if date_range:
        start, end = date_range
        df_4h  = df_4h[(df_4h.index >= start)  & (df_4h.index < end)].copy()
        df_15m = df_15m[(df_15m.index >= start) & (df_15m.index < end)].copy()

    if len(df_4h) < 50 or len(df_15m) < 200:
        return None

    df_4h  = add_emas(df_4h.copy())
    grabs  = detect_liquidity_grabs(df_4h)

    if grabs.empty:
        return None

    trades       = []
    equity       = ACCOUNT_SIZE
    equity_curve = [{"time": df_15m.index[0], "equity": equity}]
    daily_trades = {}
    daily_loss   = {}

    for _, grab in grabs.iterrows():
        grab_time = grab["grab_time"]
        grab_type = grab["grab_type"]
        direction = "long" if grab_type == "bullish" else "short"

        if grab["wick_pct"] < sym_cfg["min_wick_pct"]:
            continue
        if grab_time.hour not in sym_cfg["session_hours"]:
            continue

        try:
            slope_at_grab  = df_4h.loc[df_4h.index <= grab_time, "ema50_slope"].iloc[-1]
            ema200_at_grab = df_4h.loc[df_4h.index <= grab_time, "ema200"].iloc[-1]
        except:
            continue

        if direction == "long"  and grab["close"] < ema200_at_grab and slope_at_grab < sym_cfg["slope_long"]:
            continue
        if direction == "short" and grab["close"] > ema200_at_grab and slope_at_grab > abs(sym_cfg["slope_short"]):
            continue

        day_key = grab_time.date()
        if daily_trades.get(day_key, 0) >= MAX_TRADES_PER_DAY:
            continue
        if daily_loss.get(day_key, 0) >= DAILY_LOSS_LIMIT * equity:
            continue

        try:
            m15_start_idx = df_15m.index.searchsorted(grab_time)
        except:
            continue

        entry_window = sym_cfg.get("entry_window", ENTRY_WINDOW)
        if m15_start_idx >= len(df_15m) - entry_window:
            continue

        bos_idx = detect_bos(df_15m, m15_start_idx, direction, window=entry_window)
        if bos_idx is None:
            continue

        entry_price = sl_price = tp_price = None
        entry_idx   = None

        for j in range(bos_idx + 1, min(bos_idx + 20, len(df_15m) - 1)):
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
                if risk / entry_price > sym_cfg["max_risk_pct"]: continue
                tp_price  = entry_price + risk * MIN_RR
                entry_idx = j
                break

            elif direction == "short" and fvg_type == "bearish":
                entry_price = fvg_mid
                sl_price    = grab["high"] * (1 + 0.001)
                risk        = sl_price - entry_price
                if risk <= 0: continue
                if risk / entry_price > sym_cfg["max_risk_pct"]: continue
                tp_price  = entry_price - risk * MIN_RR
                entry_idx = j
                break

        if entry_price is None or entry_idx is None:
            continue

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

    if not trades:
        return None

    return trades, equity_curve


# ─── WALK-FORWARD VALIDATION ───────────────────────────────────────────────────

def compute_stats(trades):
    """Return dict of key metrics from trade list."""
    if not trades:
        return None
    df   = pd.DataFrame(trades)
    wins = df[df["result"] == "win"]
    loss = df[df["result"] == "loss"]

    total    = len(df)
    winrate  = len(wins) / total * 100
    total_pnl = df["pnl_usdt"].sum()
    pf_denom  = abs(loss["pnl_usdt"].sum())
    pf        = wins["pnl_usdt"].sum() / pf_denom if pf_denom > 0 else 0

    peak = ACCOUNT_SIZE; max_dd = 0
    for e in df["equity_after"].values:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd: max_dd = dd

    return {
        "total":         total,
        "winrate":       winrate,
        "total_pnl":     total_pnl,
        "profit_factor": pf,
        "avg_rr":        df["rr_achieved"].mean(),
        "max_drawdown":  max_dd,
        "final_equity":  df["equity_after"].iloc[-1],
    }


def walk_forward(symbol, df_4h, df_15m, sym_cfg, n_windows=WF_WINDOWS, train_pct=WF_TRAIN_PCT):
    """
    Rolling walk-forward: split data into n_windows, each with train/OOS split.
    Returns list of (window_idx, train_stats, oos_stats).
    """
    print(f"\n  [Walk-Forward] {symbol} | {n_windows} windows | train={int(train_pct*100)}% OOS={int((1-train_pct)*100)}%")

    all_times = df_4h.index
    t_start   = all_times[0]
    t_end     = all_times[-1]
    total_td  = t_end - t_start
    window_td = total_td / n_windows

    results = []

    for w in range(n_windows):
        win_start  = t_start + window_td * w
        win_end    = t_start + window_td * (w + 1)
        split_time = win_start + (win_end - win_start) * train_pct

        train_result = run_backtest(symbol, df_4h, df_15m, sym_cfg, date_range=(win_start, split_time))
        oos_result   = run_backtest(symbol, df_4h, df_15m, sym_cfg, date_range=(split_time, win_end))

        train_stats = compute_stats(train_result[0]) if train_result else None
        oos_stats   = compute_stats(oos_result[0])   if oos_result   else None

        status = "✓" if (oos_stats and oos_stats["profit_factor"] > 1.0) else "✗"
        t_pf   = f"{train_stats['profit_factor']:.2f}" if train_stats else "N/A"
        o_pf   = f"{oos_stats['profit_factor']:.2f}"   if oos_stats   else "N/A"
        t_tr   = train_stats["total"] if train_stats else 0
        o_tr   = oos_stats["total"]   if oos_stats   else 0

        print(f"    Window {w+1}: {win_start.date()} → {win_end.date()} | "
              f"Train PF={t_pf} ({t_tr}t) | OOS PF={o_pf} ({o_tr}t) {status}")

        results.append({
            "window":       w + 1,
            "win_start":    win_start,
            "split_time":   split_time,
            "win_end":      win_end,
            "train_stats":  train_stats,
            "oos_stats":    oos_stats,
        })

    oos_valid  = [r for r in results if r["oos_stats"] and r["oos_stats"]["profit_factor"] > 1.0]
    pass_rate  = len(oos_valid) / n_windows * 100
    avg_oos_pf = np.mean([r["oos_stats"]["profit_factor"] for r in results if r["oos_stats"]]) if results else 0

    print(f"    → OOS Pass Rate: {pass_rate:.0f}% | Avg OOS PF: {avg_oos_pf:.2f}")
    if pass_rate >= 66:
        print(f"    → VERDICT: STRATEGY VALIDATED ✓")
    elif pass_rate >= 33:
        print(f"    → VERDICT: MARGINAL — needs more data or filter tuning ⚠")
    else:
        print(f"    → VERDICT: STRATEGY FAILS OOS ✗ — likely curve-fitted")

    return results, pass_rate, avg_oos_pf


# ─── MONTE CARLO ───────────────────────────────────────────────────────────────

def monte_carlo(trades, n_runs=MC_RUNS, confidence=MC_CONFIDENCE):
    """
    Shuffle trade order n_runs times using fixed-dollar risk.
    With % equity compounding, sum is order-independent — so we use
    fixed $10 risk per trade to make path matter.
    """
    if not trades or len(trades) < 5:
        return None

    df   = pd.DataFrame(trades)

    # Recompute PnL as fixed $10 risk (makes shuffle order meaningful)
    fixed_risk = ACCOUNT_SIZE * RISK_PER_TRADE  # $10
    rr_list    = df["rr_achieved"].values.copy()
    outcomes   = np.where(df["result"].values == "win", 1, -1)
    # win = +RR * fixed_risk, loss = -fixed_risk
    fixed_pnls = np.where(outcomes == 1,
                          rr_list * fixed_risk,
                          -fixed_risk)

    # Add execution noise: ±20% RR variance (slippage, partial fills, early exits)
    rr_noise   = np.random.normal(1.0, 0.20, (n_runs, len(rr_list)))
    rr_noise   = np.clip(rr_noise, 0.3, 2.0)  # floor at 0.3R, cap at 2x

    final_equities = []
    max_drawdowns  = []
    profit_factors = []

    for run_idx in range(n_runs):
        idx      = np.random.permutation(len(fixed_pnls))
        shuffled_outcomes = outcomes[idx]
        shuffled_rr       = rr_list[idx] * rr_noise[run_idx][idx]

        noisy_pnls = np.where(shuffled_outcomes == 1,
                              shuffled_rr * fixed_risk,
                              -fixed_risk)

        equity   = ACCOUNT_SIZE
        peak     = ACCOUNT_SIZE
        max_dd   = 0
        gross_win = gross_loss = 0

        for pnl in noisy_pnls:
            equity += pnl
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
            if pnl > 0:
                gross_win  += pnl
            else:
                gross_loss += abs(pnl)

        final_equities.append(equity)
        max_drawdowns.append(max_dd)
        pf = gross_win / gross_loss if gross_loss > 0 else 0
        profit_factors.append(pf)

    alpha = 1 - confidence
    return {
        "final_equity": {
            "mean":           np.mean(final_equities),
            "median":         np.median(final_equities),
            "low":            np.percentile(final_equities, alpha / 2 * 100),
            "high":           np.percentile(final_equities, (1 - alpha / 2) * 100),
            "p5":             np.percentile(final_equities, 5),
            "p95":            np.percentile(final_equities, 95),
            "pct_profitable": np.mean([e > ACCOUNT_SIZE for e in final_equities]) * 100,
        },
        "max_drawdown": {
            "mean":  np.mean(max_drawdowns),
            "p95":   np.percentile(max_drawdowns, 95),
            "worst": np.max(max_drawdowns),
        },
        "profit_factor": {
            "mean": np.mean(profit_factors),
            "p5":   np.percentile(profit_factors, 5),
            "p95":  np.percentile(profit_factors, 95),
        },
        "all_equities":  final_equities,
        "all_drawdowns": max_drawdowns,
    }


def print_monte_carlo(symbol, mc):
    if mc is None:
        print(f"  {symbol}: Not enough trades for Monte Carlo.")
        return
    fe = mc["final_equity"]
    dd = mc["max_drawdown"]
    pf = mc["profit_factor"]

    print(f"\n  ── {symbol} Monte Carlo ({MC_RUNS} runs, {int(MC_CONFIDENCE*100)}% CI) ──")
    print(f"  Final Equity  mean=${fe['mean']:.0f}  p5=${fe['p5']:.0f}  p95=${fe['p95']:.0f}")
    print(f"  % Runs Profitable: {fe['pct_profitable']:.1f}%")
    print(f"  Max Drawdown  mean={dd['mean']:.1f}%  p95={dd['p95']:.1f}%  worst={dd['worst']:.1f}%")
    print(f"  Profit Factor mean={pf['mean']:.2f}  p5={pf['p5']:.2f}  p95={pf['p95']:.2f}")

    if fe["pct_profitable"] >= 80 and pf["p5"] > 1.0:
        print(f"  → MC VERDICT: ROBUST EDGE ✓")
    elif fe["pct_profitable"] >= 60:
        print(f"  → MC VERDICT: MODERATE EDGE ⚠")
    else:
        print(f"  → MC VERDICT: FRAGILE — high luck dependency ✗")


# ─── REPORTING ─────────────────────────────────────────────────────────────────

def print_stats(symbol, trades):
    if not trades:
        print(f"  {symbol}: No trades.")
        return
    stats = compute_stats(trades)
    df    = pd.DataFrame(trades)
    longs  = df[df["direction"] == "long"]
    shorts = df[df["direction"] == "short"]

    print(f"\n  ── {symbol} Full-Sample Results ──")
    print(f"  Total trades:    {stats['total']}")
    print(f"  Win Rate:        {stats['winrate']:.1f}%")
    print(f"  Total PnL:       ${stats['total_pnl']:.2f}")
    print(f"  Profit Factor:   {stats['profit_factor']:.2f}")
    print(f"  Avg RR:          {stats['avg_rr']:.2f}")
    print(f"  Max Drawdown:    {stats['max_drawdown']:.1f}%")
    print(f"  Final Equity:    ${stats['final_equity']:.2f}")

    if len(longs)  > 0:
        lwr = len(longs[longs['result']=='win']) / len(longs) * 100
        print(f"  Long WR:         {lwr:.1f}% ({len(longs)} trades)")
    if len(shorts) > 0:
        swr = len(shorts[shorts['result']=='win']) / len(shorts) * 100
        print(f"  Short WR:        {swr:.1f}% ({len(shorts)} trades)")


def _scalar(val):
    if isinstance(val, pd.Series):
        if len(val) == 0: return None
        return float(val.iloc[-1])
    return float(val)


# ─── PLOTTING ──────────────────────────────────────────────────────────────────

def plot_results(all_results, wf_data, mc_data):
    n   = len(all_results)
    fig = plt.figure(figsize=(16, 6 * n + 4 * n))

    BG    = "#0d1117"
    PANEL = "#161b22"
    GREEN = "#00d4aa"
    RED   = "#ff4444"
    BLUE  = "#4fc3f7"
    GOLD  = "#ffd700"
    GRAY  = "#aaaaaa"

    fig.patch.set_facecolor(BG)
    fig.suptitle("4H Liquidity Grab + 15M SMC Entry — Delta Exchange",
                 fontsize=15, fontweight="bold", color="#eee", y=0.98)

    row = 0
    total_rows = n * 3  # equity + wf + mc per symbol

    for sym_idx, (symbol, trades, equity_curve) in enumerate(all_results):
        # ── Row 1: Equity Curve ────────────────────────────────────
        ax1 = fig.add_subplot(total_rows, 1, row + 1)
        row += 1

        if trades:
            df_t  = pd.DataFrame(trades)
            eq_df = pd.DataFrame(equity_curve).dropna(subset=["time"]).set_index("time").sort_index()
            eq_df = eq_df[~eq_df.index.duplicated(keep="last")]

            ax1.fill_between(eq_df.index, ACCOUNT_SIZE, eq_df["equity"],
                             where=(eq_df["equity"] >= ACCOUNT_SIZE),
                             color=GREEN, alpha=0.15)
            ax1.fill_between(eq_df.index, ACCOUNT_SIZE, eq_df["equity"],
                             where=(eq_df["equity"] < ACCOUNT_SIZE),
                             color=RED, alpha=0.15)
            ax1.plot(eq_df.index, eq_df["equity"], color=GREEN, linewidth=2, label="Equity")
            ax1.axhline(y=ACCOUNT_SIZE, color="#555", linestyle="--", linewidth=1, label="Start")

            for _, row_t in df_t.iterrows():
                t = row_t["exit_time"]
                if pd.isnull(t) or t not in eq_df.index: continue
                eq_val = _scalar(eq_df.loc[t, "equity"])
                if eq_val is None: continue
                c = GREEN if row_t["result"] == "win" else RED
                ax1.scatter(t, eq_val, color=c, s=25, zorder=5)

            stats = compute_stats(trades)
            ax1.set_title(
                f"{symbol} Equity  |  Trades: {stats['total']}  |  WR: {stats['winrate']:.1f}%  "
                f"|  PF: {stats['profit_factor']:.2f}  |  PnL: ${stats['total_pnl']:.2f}",
                fontsize=11, color="#eee"
            )

        ax1.set_facecolor(PANEL)
        ax1.set_ylabel("Equity (USDT)", color=GRAY)
        ax1.tick_params(colors=GRAY)
        for sp in ["top","right"]: ax1.spines[sp].set_visible(False)
        for sp in ["bottom","left"]: ax1.spines[sp].set_color("#333")
        ax1.legend(loc="upper left", facecolor=PANEL, edgecolor="#333", labelcolor="#ccc")
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax1.grid(axis="y", color="#1e1e2e", linewidth=0.5)

        # ── Row 2: Walk-Forward OOS PF bars ───────────────────────
        ax2 = fig.add_subplot(total_rows, 1, row + 1)
        row += 1

        if symbol in wf_data:
            wf_results, pass_rate, avg_oos_pf = wf_data[symbol]
            windows = [r["window"] for r in wf_results]
            train_pfs = [r["train_stats"]["profit_factor"] if r["train_stats"] else 0 for r in wf_results]
            oos_pfs   = [r["oos_stats"]["profit_factor"]   if r["oos_stats"]   else 0 for r in wf_results]

            x     = np.arange(len(windows))
            width = 0.35
            bars1 = ax2.bar(x - width/2, train_pfs, width, label="Train PF", color=BLUE,   alpha=0.7)
            bars2 = ax2.bar(x + width/2, oos_pfs,   width, label="OOS PF",   color=GOLD,   alpha=0.7)
            ax2.axhline(y=1.0, color=RED,   linestyle="--", linewidth=1, label="PF=1.0")
            ax2.axhline(y=1.5, color=GREEN, linestyle=":",  linewidth=1, label="PF=1.5")

            for bar in bars2:
                h = bar.get_height()
                c = GREEN if h >= 1.0 else RED
                ax2.text(bar.get_x() + bar.get_width()/2., h + 0.02,
                         f"{h:.2f}", ha="center", va="bottom", fontsize=8, color=c)

            ax2.set_xticks(x)
            ax2.set_xticklabels([f"W{w}" for w in windows], color=GRAY)
            verdict = "VALIDATED ✓" if pass_rate >= 66 else ("MARGINAL ⚠" if pass_rate >= 33 else "FAILS ✗")
            ax2.set_title(
                f"{symbol} Walk-Forward  |  OOS Pass: {pass_rate:.0f}%  |  Avg OOS PF: {avg_oos_pf:.2f}  |  {verdict}",
                fontsize=11, color="#eee"
            )
            ax2.legend(facecolor=PANEL, edgecolor="#333", labelcolor="#ccc", fontsize=8)

        ax2.set_facecolor(PANEL)
        ax2.set_ylabel("Profit Factor", color=GRAY)
        ax2.tick_params(colors=GRAY)
        for sp in ["top","right"]: ax2.spines[sp].set_visible(False)
        for sp in ["bottom","left"]: ax2.spines[sp].set_color("#333")
        ax2.grid(axis="y", color="#1e1e2e", linewidth=0.5)

        # ── Row 3: Monte Carlo distribution ───────────────────────
        ax3 = fig.add_subplot(total_rows, 1, row + 1)
        row += 1

        if symbol in mc_data and mc_data[symbol]:
            mc = mc_data[symbol]
            fe = mc["all_equities"]
            ax3.hist(fe, bins=60, color=BLUE, alpha=0.7, edgecolor="none")
            ax3.axvline(x=ACCOUNT_SIZE,          color=RED,   linestyle="--", linewidth=1.5, label=f"Start ${ACCOUNT_SIZE}")
            ax3.axvline(x=mc["final_equity"]["p5"],  color=GOLD,  linestyle=":",  linewidth=1.5, label=f"p5  ${mc['final_equity']['p5']:.0f}")
            ax3.axvline(x=mc["final_equity"]["p95"], color=GREEN, linestyle=":",  linewidth=1.5, label=f"p95 ${mc['final_equity']['p95']:.0f}")
            ax3.axvline(x=mc["final_equity"]["mean"],color="#fff", linestyle="-",  linewidth=1,   label=f"mean ${mc['final_equity']['mean']:.0f}")

            pct = mc["final_equity"]["pct_profitable"]
            pf5 = mc["profit_factor"]["p5"]
            verdict = "ROBUST ✓" if (pct >= 80 and pf5 > 1.0) else ("MODERATE ⚠" if pct >= 60 else "FRAGILE ✗")
            ax3.set_title(
                f"{symbol} Monte Carlo ({MC_RUNS} runs)  |  {pct:.1f}% profitable  "
                f"|  p5 PF={pf5:.2f}  |  Worst DD={mc['max_drawdown']['worst']:.1f}%  |  {verdict}",
                fontsize=11, color="#eee"
            )
            ax3.legend(facecolor=PANEL, edgecolor="#333", labelcolor="#ccc", fontsize=8)

        ax3.set_facecolor(PANEL)
        ax3.set_xlabel("Final Equity (USDT)", color=GRAY)
        ax3.set_ylabel("Frequency", color=GRAY)
        ax3.tick_params(colors=GRAY)
        for sp in ["top","right"]: ax3.spines[sp].set_visible(False)
        for sp in ["bottom","left"]: ax3.spines[sp].set_color("#333")
        ax3.grid(axis="y", color="#1e1e2e", linewidth=0.5)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig("backtest_results.png", dpi=150, bbox_inches="tight", facecolor=BG)
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
    print("  Validation: Walk-Forward + Monte Carlo")
    print("="*60)

    exchange    = get_exchange()
    all_results = []
    wf_data     = {}
    mc_data     = {}

    print("\nFetching OHLCV data...")

    for symbol_key in SYMBOLS:
        print(f"\n[{symbol_key}]")
        df_4h,  sym_4h = fetch_ohlcv(exchange, symbol_key, "4h",  LOOKBACK_DAYS)
        df_15m, _      = fetch_ohlcv(exchange, symbol_key, "15m", LOOKBACK_DAYS)

        if df_4h is None or df_15m is None:
            print(f"  Skipping {symbol_key} — fetch failed")
            continue
        if len(df_4h) < 50 or len(df_15m) < 200:
            print(f"  Skipping {symbol_key} — insufficient data")
            continue

        cfg = SYMBOL_CONFIG.get(symbol_key, SYMBOL_CONFIG["ETHUSDT"])

        # Full-sample backtest
        print(f"\n{'='*60}")
        print(f"  BACKTEST: {symbol_key}")
        print(f"{'='*60}")
        result = run_backtest(symbol_key, df_4h, df_15m, sym_cfg=cfg)
        if result is None:
            print(f"  No trades generated.")
            continue

        trades, equity_curve = result
        print_stats(symbol_key, trades)
        all_results.append((symbol_key, trades, equity_curve))

        # Walk-forward
        wf_results = walk_forward(symbol_key, df_4h, df_15m, cfg)
        wf_data[symbol_key] = wf_results

        # Monte Carlo
        print(f"\n  [Monte Carlo] {symbol_key} | {MC_RUNS} runs...")
        mc = monte_carlo(trades)
        mc_data[symbol_key] = mc
        print_monte_carlo(symbol_key, mc)

    if all_results:
        print("\n" + "="*60)
        print("  Generating charts...")
        plot_results(all_results, wf_data, mc_data)
        export_trades(all_results)
        print("\n  Done. Check backtest_results.png and backtest_trades.csv")
    else:
        print("\n  No results to display.")


if __name__ == "__main__":
    main()