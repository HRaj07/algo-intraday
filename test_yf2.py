import yfinance as yf
import requests

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

try:
    df = yf.download("HDFCBANK.NS", period="5d", interval="15m", session=session)
    print(df)
except Exception as e:
    print(e)
