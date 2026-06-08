# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

**Trading Bot: 4H Liquidity Grab + 15M SMC Entry Strategy**

Three entry points, one shared strategy engine:

1. **back_test.py** — Historical backtest (6 months, 2020-2025 data)
   - Defines strategy logic: liquidity grab detection, BOS (break of structure), FVG (fair value gap) entry
   - Reusable functions: `add_emas()`, `detect_liquidity_grabs()`, `detect_bos()`, `detect_fvg()`
   - Exports detection results to `backtest_trades.csv` + chart visualization
   - **Key config:** SYMBOL_CONFIG (per-symbol wick %, session hours, slope thresholds), MIN_RR=3:1, RISK_PER_TRADE=1%

2. **live_trade.py** — Forward-test on Delta demo (paper trading, 24/7)
   - Imports detection functions from back_test.py (does NOT duplicate them)
   - Warm-starts with historical grab detection (marks all existing grabs as "seen" to avoid stale signals)
   - Polls every 15m candle close, detects fresh signals, auto-places bracket orders
   - Enforces same daily limits (2 trades/day, 3% daily loss max)
   - One trade per symbol at a time
   - **Exit price:** fetches actual fill price from exchange (`fetch_my_trades` → `fetch_closed_orders` → ticker fallback)
   - **Duplicate prevention:** tracks logged trades by `sym_key + opened_at` to avoid double CSV entries
   - Logs trades to `trades_log.csv` (fields: date, symbol, side, lots, contract_size, entry/exit/sl/tp prices, pnl_usd, result, hold_time, timestamps)

3. **monitor_trade.py** — Standalone position watcher
   - Polls open positions every 30s
   - Tracks live uPnL
   - When position closes (SL/TP hit), logs entry/exit/PnL to `trades_log.csv`
   - Can run independently of live_trade.py

## Exchange Integration

**Delta Exchange (India) + CCXT**

Critical: Delta India demo API host differs from ccxt's default global testnet.

```python
ex = ccxt.delta({"apiKey": key, "secret": sec})
ex.urls['api'] = {'public': 'https://cdn-ind.testnet.deltaex.org', 'private': 'https://cdn-ind.testnet.deltaex.org'}
```

**Symbol mapping (strategy code → Delta live):**
- Code uses "BTCUSDT" / "ETHUSDT" (matches backtest data)
- Delta demo has USD-settled perps: BTC/USD:USD, ETH/USD:USD (not USDT)
- Conversion in live_trade.py: `SYMBOL_MAP = {"BTCUSDT": {"ccxt": "BTC/USD:USD", "contract_size": 0.001}, ...}`

**Contract sizing (perps, not spot):**
- BTC/USD:USD: 1 lot = 0.001 BTC
- ETH/USD:USD: 1 lot = 0.01 ETH
- Amount in ccxt = number of lots (integer)
- Formula: `risk_amount = equity * 0.01; lots = max(1, round((risk_amount / sl_distance) / contract_size))`

**Order mechanics:**
- Entry: `ex.create_order(symbol, 'market', side, lots)`
- Bracket SL+TP: attach to entry via params on create_order:
  ```python
  params={
    'bracket_stop_loss_price': str(sl),
    'bracket_stop_loss_limit_price': str(sl),
    'bracket_take_profit_price': str(tp),
    'bracket_take_profit_limit_price': str(tp),
  }
  ```
- Bracket orders appear as separate reduceOnly limit orders (visible in fetch_open_orders)
- To manually close: cancel bracket orders first, then market close with `reduce_only: True`

## Running

**Setup:**
```bash
pip install -r requirements.txt
# .env must have: API_KEY, API_SCECRET (in plaintext, already in .gitignore)
```

**Commands:**
```bash
# Backtest (~3min, generates backtest_results.png + backtest_trades.csv)
python back_test.py

# Live paper trader (runs forever, polls every 15m, places orders on demo)
python live_trade.py

# Position monitor (polls every 30s, logs closes to trades_log.csv)
python monitor_trade.py

# Logs
tail -f live_trade.log        # live_trade.py activity
cat trades_log.csv             # all completed trades
```

## Key Files & State

- `.env`: API keys (DO NOT COMMIT, already .gitignored)
- `back_test.py`: strategy engine + config (SYMBOL_CONFIG, MIN_RR, etc)
- `live_trade.py`: reuses back_test functions, adds warm-start + position tracking
- `monitor_trade.py`: independent position watcher
- `backtest_trades.csv`: historical trade results (from back_test.py)
- `trades_log.csv`: live/demo trade results (appended by live_trade & monitor_trade)
- `graphify-out/`: codebase graph (graph.html, graph.json)
- `claude/DEMO_NOTES.md`: Delta India demo connection details + sizing formulas
- `claude/STRATEGY.md`: Master strategy rules (outdated, references Binance not Delta)

## Strategy Recall

**Entry Setup (all 3 must align):**
1. **4H Liquidity Grab:** Wick beyond swing high/low + close rejection (bearish grab = short bias, bullish = long bias)
   - Grab validity: wick % ≥ min_wick_pct (varies by symbol: BTC 0.4%, ETH 0.3%)
   - Session filter: skip low-liquidity hours (varies: BTC UTC 6-23, ETH 0-24)
   - Trend filter: EMA50 slope okay for direction (slope_long/slope_short thresholds)

2. **15M Break of Structure (BOS):** After grab, within 16 candles (4h), find close breaking recent swing
   - Long: close > recent 3-candle high
   - Short: close < recent 3-candle low

3. **15M Fair Value Gap (FVG) Entry:** After BOS, within 8 candles, find 3-candle gap
   - Bullish FVG: candle[i-2].high < candle[i].low (gap up)
   - Bearish FVG: candle[i-2].low > candle[i].high (gap down)
   - Entry at FVG midpoint

**Risk:**
- SL: beyond grab wick + 0.5× ATR(14)
- TP: entry ± (risk × MIN_RR), MIN_RR = 3:1 minimum
- Reject if risk/entry > max_risk_pct (symbol-specific, ~1.2-2%)
- Position size = 1% account risk / SL distance

**Limits:**
- Max 2 trades per day per symbol
- Max 3% daily loss → stop trading rest of day
- One position per symbol at a time

## Common Gotchas

1. **Symbol mismatch:** back_test uses BTCUSDT aliases; live_trade converts to BTC/USD:USD. Don't mix.
2. **Contract sizing:** Lots are integer. Fractional amounts round up or fail. Min 1 lot.
3. **Bracket orders:** SL/TP are limit orders (not market). If they don't fill they stay open. For demo this is fine.
4. **Warm start:** live_trade marks all historical grabs as "seen" on first run to avoid firing 180 days of trades at startup.
5. **Candle timing:** API fetch returns latest closed candle. Signal detection only triggers on fresh candles (last 2-3). Stale BOS indices are skipped.
6. **API rate limits:** ccxt.delta has `enableRateLimit: True`. Fetches are throttled (~100ms/call). Backtest is slow (~3min for 180d).
7. **Exit price accuracy:** Previously used `fetch_ticker` (live market price) as exit — this gave wrong PnL since the price moves after SL/TP fills. Now uses `_fetch_exit_price()` which queries actual trade fills from the exchange. If exchange APIs fail, falls back to ticker with a log warning.
8. **Duplicate CSV rows:** PosTracker detects position closure when `has_open_position()` returns False. If the poll runs twice before the trade is cleaned up, the same trade could be logged twice. The `_logged` set prevents this.

## Debugging

- **"invalid_api_key":** Check .env keys are from Delta demo (not production). Verify host override worked.
- **"no_position_for_reduce_only":** Bracket SL/TP orders reserve the position. Cancel them before a second reduce-only order.
- **No trades on live_trade.py:** Warm-start marked all grabs as seen. Wait for NEW grabs (takes hours) or test with --mode deep detection.
- **Monitor_trade.py shows "uPnL None":** Exchange didn't return unrealizedPnl field. Falls back to manual calc; acceptable.
- **Chart not generated:** matplotlib may need display. Run on headless: `python back_test.py > /dev/null` still exports CSV + chart.
