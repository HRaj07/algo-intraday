import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS"
]

data_1h = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="730d", interval="1h", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None: df.index = df.index.tz_localize(None)
            data_1h[t] = df.dropna()
    except Exception: pass

def rsi_calc(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs)).fillna(50)

def vwap_hourly(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

precalc = {}
for t, df in data_1h.items():
    precalc[t] = {
        "rsi": rsi_calc(df['close'], 14),
        "vwap": vwap_hourly(df),
    }

all_days = sorted(set(list(data_1h.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

print("Testing Intraday VWAP Mean Reversion Strategy (Dip-Buying below VWAP with RSI < 30)...")

trades = []
daily_pnl = defaultdict(float)
yearly_pnl = defaultdict(float)

for day in all_days:
    day_signals = []
    for t, df in data_1h.items():
        today = df[df.index.date == day]
        if len(today) < 3: continue
        
        rsi_series = precalc[t]['rsi']
        vw_series = precalc[t]['vwap']
        
        # Scan intraday bars 1 to 4 (10:15 - 1:15)
        for i in range(1, min(5, len(today))):
            bar = today.iloc[i]
            prev = today.iloc[i-1]
            vw_val = vw_series.loc[today.index[i]]
            rsi_val = rsi_series.loc[today.index[i]]
            
            # Oversold condition: Price pulls back > 1% below VWAP and RSI < 32
            dev_from_vwap = (vw_val - bar['close']) / vw_val
            if dev_from_vwap > 0.008 and rsi_val < 35 and bar['close'] > bar['open']:
                # Bullish hammer/reversal candle below VWAP
                stop = bar['low'] * 0.995
                risk = bar['close'] - stop
                if risk > 0:
                    target = vw_val # Target is mean reversion back to VWAP
                    if (target - bar['close']) >= 1.2 * risk:
                        day_signals.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': dev_from_vwap, 'risk': risk
                        })
                break

    selected = sorted(day_signals, key=lambda x: x['score'], reverse=True)[:1]
    
    for sig in selected:
        t = sig['ticker']
        df = data_1h[t]
        today = df[df.index.date == day]
        entry, stop, target, direction, bar_idx, risk = sig['entry'], sig['stop'], sig['target'], sig['dir'], sig['bar_idx'], sig['risk']
        qty = max(1, int(RISK_PER_TRADE / risk))
        curr_stop = stop
        exit_price = None
        result = 'squareoff'
        
        for j in range(bar_idx + 1, len(today)):
            bar = today.iloc[j]
            if j == len(today) - 1:
                exit_price = bar['close']
                result = 'squareoff'
                break
                
            if bar['low'] <= curr_stop:
                exit_price = curr_stop
                result = 'stop'
                break
            if bar['high'] >= target:
                exit_price = target
                result = 'target'
                break
                    
        if exit_price is None: continue
        pnl = (exit_price - entry) * qty
        net_pnl = pnl - COST_PER_TRADE
        trades.append({"net_pnl": net_pnl, "result": result, "ticker": t, "date": str(day), "dir": direction})
        daily_pnl[day] += net_pnl
        yearly_pnl[day.year] += net_pnl

wins = [t for t in trades if t['net_pnl'] > 0]
total_net = sum(t['net_pnl'] for t in trades)
win_rate = len(wins) / len(trades) * 100 if trades else 0
gross_win = sum(t['net_pnl'] for t in wins)
gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
pf = gross_win / gross_loss if gross_loss > 0 else 99

print(f"\n[VWAP Mean Reversion Strategy]")
print(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f} | PF: {pf:.2f}")
print(f"  Yearly: " + ", ".join([f"{y}: ₹{p:+,.0f}" for y, p in sorted(yearly_pnl.items())]))
