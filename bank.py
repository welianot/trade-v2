import yfinance as yf
# BANKNIFTY proxy
df = yf.download("^NSEBANK", period="6mo", interval="5m")
print(df)