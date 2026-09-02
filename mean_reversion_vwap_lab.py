import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "TITAN.NS", "SUNPHARMA.NS", "MARUTI.NS", "M&M.NS"
]

print("Fetching data...", flush=True)
df_1h = yf.download(TICKERS, period="730d", interval="1h", auto_adjust=True, progress=False, group_by='ticker')

data_1h = {}
for t in TICKERS:
    try:
        sub = df_1h[t].dropna()
        sub.columns = [c.lower() for c in sub.columns]
        if sub.index.tz is not None: sub.index = sub.index.tz_localize(None)
        if len(sub) > 50: data_1h[t] = sub
    except Exception: pass

def vwap_hourly(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

def rsi_calc(df, period=14):
    close = df['close']
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs)).fillna(50)

precalc = {}
for t, df in data_1h.items():
    precalc[t] = {
        "vwap": vwap_hourly(df),
        "rsi": rsi_calc(df),
    }

sample_t = list(data_1h.keys())[0]
all_days = sorted(set(data_1h[sample_t].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

def test_mr(name, rsi_os=30, rsi_ob=70, max_trades=2):
    trades = []
    yearly_pnl = defaultdict(float)

    for day in all_days:
        candidates = []
        for t, df in data_1h.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            vw = precalc[t]['vwap']
            rsi = precalc[t]['rsi']
            
            for i in range(1, len(today) - 1):
                bar = today.iloc[i]
                vw_val = vw.loc[today.index[i]]
                rsi_val = rsi.loc[today.index[i]]
                
                # Buy when RSI < 30 and price is at least 0.8% below VWAP (oversold extension)
                if rsi_val < rsi_os and (vw_val - bar['close']) / bar['close'] > 0.008:
                    entry = bar['close']
                    stop = bar['low'] * 0.993 # 0.7% stop
                    risk = entry - stop
                    target = vw_val # target is mean reversion to VWAP
                    if target > entry and risk > 0:
                        candidates.append({
                            'ticker': t, 'dir': 'LONG', 'entry': entry, 'stop': stop,
                            'target': target, 'bar_idx': i, 'risk': risk, 'score': rsi_os - rsi_val
                        })
                    break
                    
                # Short when RSI > 70 and price is at least 0.8% above VWAP
                elif rsi_val > rsi_ob and (bar['close'] - vw_val) / vw_val > 0.008:
                    entry = bar['close']
                    stop = bar['high'] * 1.007 # 0.7% stop
                    risk = stop - entry
                    target = vw_val
                    if target < entry and risk > 0:
                        candidates.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': entry, 'stop': stop,
                            'target': target, 'bar_idx': i, 'risk': risk, 'score': rsi_val - rsi_ob
                        })
                    break

        selected = sorted(candidates, key=lambda x: x['score'], reverse=True)[:max_trades]
        for sig in selected:
            t = sig['ticker']
            df = data_1h[t]
            today = df[df.index.date == day]
            entry, stop, target, direction, bar_idx, risk = sig['entry'], sig['stop'], sig['target'], sig['dir'], sig['bar_idx'], sig['risk']
            qty = max(1, int(RISK_PER_TRADE / risk))
            exit_p = None
            
            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_p = bar['close']
                    break
                if direction == 'LONG':
                    if bar['low'] <= stop: exit_p = stop; break
                    if bar['high'] >= target: exit_p = target; break
                else:
                    if bar['high'] >= stop: exit_p = stop; break
                    if bar['low'] <= target: exit_p = target; break
                    
            if exit_p is None: continue
            pnl = (exit_p - entry) * qty if direction == 'LONG' else (entry - exit_p) * qty
            net_pnl = pnl - COST_PER_TRADE
            trades.append(net_pnl)
            yearly_pnl[day.year] += net_pnl

    wins = [t for t in trades if t > 0]
    total_net = sum(trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_win = sum(wins)
    gross_loss = abs(sum(t for t in trades if t <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99

    print(f"[{name}]")
    print(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f} | PF: {pf:.2f}")
    for y, p in sorted(yearly_pnl.items()):
        print(f"    {y}: ₹{p:+,.0f}", end=" | ")
    print("\n")

test_mr("1. VWAP Mean Reversion (RSI < 30 / > 70)", rsi_os=30, rsi_ob=70)
test_mr("2. Extreme VWAP Mean Reversion (RSI < 25 / > 75)", rsi_os=25, rsi_ob=75)
