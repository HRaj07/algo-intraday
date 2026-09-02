import yfinance as yf
print(yf.__version__)
df = yf.download("HDFCBANK.NS", period="5d", interval="15m")
print(df)
df = yf.download("AAPL", period="5d", interval="15m")
print(df)
