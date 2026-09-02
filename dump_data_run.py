import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict
import os
import pickle
import warnings
warnings.filterwarnings('ignore')

CACHE_FILE = "/Users/harshit/Documents/StreamFab/algo-intraday/cache_730d.pkl"
TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "TITAN.NS", "SUNPHARMA.NS", "MARUTI.NS", "M&M.NS"
]

if os.path.exists(CACHE_FILE):
    print("Loading from pickle cache...", flush=True)
    with open(CACHE_FILE, "rb") as f:
        data_1h = pickle.load(f)
else:
    print("Downloading 730d dataset and caching...", flush=True)
    df_1h = yf.download(TICKERS, period="730d", interval="1h", auto_adjust=True, progress=False, group_by='ticker')
    data_1h = {}
    for t in TICKERS:
        try:
            sub = df_1h[t].dropna()
            sub.columns = [c.lower() for c in sub.columns]
            if sub.index.tz is not None: sub.index = sub.index.tz_localize(None)
            if len(sub) > 50: data_1h[t] = sub
        except Exception: pass
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(data_1h, f)

print(f"Data ready with {len(data_1h)} tickers.", flush=True)

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

def bb_calc(df, period=20, std_dev=2.0):
    mid = df['close'].rolling(period).mean()
    std = df['close'].rolling(period).std()
    return mid + std_dev * std, mid - std_dev * std

precalc = {}
for t, df in data_1h.items():
    bb_u, bb_l = bb_calc(df)
    precalc[t] = {
        "vwap": vwap_hourly(df),
        "rsi": rsi_calc(df, 14),
        "bb_u": bb_u,
        "bb_l": bb_l,
    }

sample_t = list(data_1h.keys())[0]
all_days = sorted(set(data_1h[sample_t].index.date))
COST = 50
RISK = 2000

def test_engine(name, rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=False, bb=False, max_trades=2):
    trades = []
    yearly = defaultdict(float)

    for day in all_days:
        cands = []
        for t, df in data_1h.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            vw = precalc[t]['vwap'].loc[today.index]
            rsi = precalc[t]['rsi'].loc[today.index]
            bb_u = precalc[t]['bb_u'].loc[today.index]
            bb_l = precalc[t]['bb_l'].loc[today.index]

            for i in range(1, len(today) - 1):
                c_p = today.iloc[i]['close']
                vw_p = vw.iloc[i]
                r_val = rsi.iloc[i]
                
                # LONG
                if r_val <= rsi_os and (vw_p - c_p) / c_p >= dev:
                    if bb and c_p > bb_l.iloc[i]: continue
                    entry = c_p
                    stop = entry * (1.0 - stop_pct)
                    risk = entry - stop
                    if risk > 0:
                        cands.append({'t': t, 'dir': 'LONG', 'entry': entry, 'stop': stop, 'target': vw_p, 'idx': i, 'risk': risk, 'score': (rsi_os - r_val) + ((vw_p - c_p)/c_p * 100)})
                    break
                    
                # SHORT
                if r_val >= rsi_ob and (c_p - vw_p) / vw_p >= dev:
                    if bb and c_p < bb_u.iloc[i]: continue
                    entry = c_p
                    stop = entry * (1.0 + stop_pct)
                    risk = stop - entry
                    if risk > 0:
                        cands.append({'t': t, 'dir': 'SHORT', 'entry': entry, 'stop': stop, 'target': vw_p, 'idx': i, 'risk': risk, 'score': (r_val - rsi_ob) + ((c_p - vw_p)/vw_p * 100)})
                    break

        sel = sorted(cands, key=lambda x: x['score'], reverse=True)[:max_trades]
        for s in sel:
            t = s['t']
            today = data_1h[t][data_1h[t].index.date == day]
            entry, stop, target, direction, idx, risk = s['entry'], s['stop'], s['target'], s['dir'], s['idx'], s['risk']
            qty = max(1, int(RISK / risk))
            curr_stop = stop
            exit_p = None
            
            for j in range(idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_p = bar['close']
                    break
                if trail:
                    if direction == 'LONG' and bar['high'] >= entry + 0.8 * risk:
                        curr_stop = max(curr_stop, entry + 0.1 * risk)
                    elif direction == 'SHORT' and bar['low'] <= entry - 0.8 * risk:
                        curr_stop = min(curr_stop, entry - 0.1 * risk)
                        
                if direction == 'LONG':
                    if bar['low'] <= curr_stop: exit_p = curr_stop; break
                    if bar['high'] >= target: exit_p = target; break
                else:
                    if bar['high'] >= curr_stop: exit_p = curr_stop; break
                    if bar['low'] <= target: exit_p = target; break
                    
            if exit_p is None: continue
            pnl = (exit_p - entry) * qty if direction == 'LONG' else (entry - exit_p) * qty
            net = pnl - COST
            trades.append(net)
            yearly[day.year] += net

    wins = [t for t in trades if t > 0]
    total_net = sum(trades)
    wr = len(wins)/len(trades)*100 if trades else 0
    gross_win = sum(wins)
    gross_loss = abs(sum(t for t in trades if t <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99
    
    print(f"\n==================================================")
    print(f"  {name}")
    print(f"==================================================")
    print(f"  Trades: {len(trades)} | Win Rate: {wr:.1f}% | Profit Factor: {pf:.2f}")
    print(f"  Net P&L: ₹{total_net:+,.0f}")
    yr_str = " | ".join([f"{y}: ₹{p:+,.0f}" for y, p in sorted(yearly.items())])
    print(f"  Yearly : {yr_str}")
    print(f"==================================================", flush=True)

test_engine("1. Baseline: RSI < 25 / > 75 | Dev > 0.8% | Stop 0.8% | Max 2 Trades",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=False, bb=False, max_trades=2)

test_engine("2. + Halfway Breakeven Trailing Stop",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=True, bb=False, max_trades=2)

test_engine("3. + Bollinger Band Confluence (Touch Lower/Upper BB)",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=False, bb=True, max_trades=2)

test_engine("4. 🌟 Ultimate Triple Confluence (RSI + VWAP + BB + Trail)",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=True, bb=True, max_trades=2)

test_engine("5. 🌟 High Conviction Single Slot (Max 1 Trade/Day, BB + Trail)",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=True, bb=True, max_trades=1)

test_engine("6. Deep Extension Filter (Dev > 1.0% + BB + Trail)",
            rsi_os=25, rsi_ob=75, dev=0.010, stop_pct=0.008, trail=True, bb=True, max_trades=2)
