# stock_scanner.py

import pandas as pd


STOCKS = [
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:INFY-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:SBIN-EQ",
    "NSE:LT-EQ",
    "NSE:ITC-EQ",
    "NSE:BHARTIARTL-EQ",
    "NSE:AXISBANK-EQ",
    "NSE:KOTAKBANK-EQ",
    "NSE:MARUTI-EQ",
    "NSE:M&M-EQ",
    "NSE:TATAMOTORS-EQ",
    "NSE:TATASTEEL-EQ",
    "NSE:HINDUNILVR-EQ",
    "NSE:ASIANPAINT-EQ",
    "NSE:BAJFINANCE-EQ",
    "NSE:BAJAJFINSV-EQ",
    "NSE:ULTRACEMCO-EQ",
    "NSE:POWERGRID-EQ",
    "NSE:NTPC-EQ",
    "NSE:ADANIENT-EQ",
    "NSE:ADANIPORTS-EQ",
    "NSE:WIPRO-EQ",
    "NSE:TECHM-EQ",
    "NSE:HCLTECH-EQ",
    "NSE:SUNPHARMA-EQ",
    "NSE:DRREDDY-EQ",
    "NSE:CIPLA-EQ"
]


def scan_stocks(fyers):

    bullish = []
    bearish = []

    for symbol in STOCKS:

        try:
            data = {
                "symbol": symbol,
                "resolution": "D",
                "date_format": "1",
                "range_from": "2026-01-01",
                "range_to": "2026-12-31",
                "cont_flag": "1"
            }

            hist = fyers.history(data)

            candles = hist.get("candles", [])

            if len(candles) < 60:
                continue

            df = pd.DataFrame(
                candles,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume"
                ]
            )

            df["ema20"] = df["close"].ewm(span=20).mean()
            df["ema50"] = df["close"].ewm(span=50).mean()

            last = df.iloc[-1]

            price = last["close"]

            if last["ema20"] > last["ema50"]:
                bullish.append(
                    f"🟢 {symbol.replace('NSE:','').replace('-EQ','')} | ₹{price:.2f}"
                )
            else:
                bearish.append(
                    f"🔴 {symbol.replace('NSE:','').replace('-EQ','')} | ₹{price:.2f}"
                )

        except Exception as e:
            print(f"Error scanning {symbol}: {e}")

    result = "📈 BULLISH STOCKS\n\n"

    if bullish:
        result += "\n".join(bullish)
    else:
        result += "None"

    result += "\n\n📉 BEARISH STOCKS\n\n"

    if bearish:
        result += "\n".join(bearish)
    else:
        result += "None"

    return result