import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "TITAN.NS", "SUNPHARMA.NS", "MARUTI.NS", "M&M.NS"
]

print("Downloading dataset...", flush=True)
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

def bb_calc(df, period=20, std_dev=2.0):
    mid = df['close'].rolling(period).mean()
    std = df['close'].rolling(period).std()
    return mid + std_dev * std, mid - std_dev * std

# Prepare fast records per day
all_days = sorted(set(data_1h[TICKERS[0]].index.date))
days_data = []

for day in all_days:
    d_tickers = {}
    for t in TICKERS:
        if t not in data_1h: continue
        df = data_1h[t]
        today = df[df.index.date == day]
        if len(today) < 4: continue
        
        vw = vwap_hourly(df).loc[today.index]
        rsi = rsi_calc(df).loc[today.index]
        bb_u, bb_l = bb_calc(df)
        bb_u_sub = bb_u.loc[today.index]
        bb_l_sub = bb_l.loc[today.index]
        
        d_tickers[t] = {
            "today": today,
            "vwap": vw.values,
            "rsi": rsi.values,
            "bb_u": bb_u_sub.values,
            "bb_l": bb_l_sub.values,
            "close": today['close'].values,
            "high": today['high'].values,
            "low": today['low'].values,
            "len": len(today)
        }
    days_data.append((day, d_tickers))

COST = 50
RISK = 2000

def run_sim(name, rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=False, bb=False, max_trades=2):
    trades = []
    yearly = defaultdict(float)
    
    for day, d_tickers in days_data:
        cands = []
        for t, d in d_tickers.items():
            n = d['len']
            close, high, low, vwap, rsi, bb_u, bb_l = d['close'], d['high'], d['low'], d['vwap'], d['rsi'], d['bb_u'], d['bb_l']
            
            for i in range(1, n - 1):
                c_p = close[i]
                vw_p = vwap[i]
                r_val = rsi[i]
                
                # LONG
                if r_val <= rsi_os and (vw_p - c_p) / c_p >= dev:
                    if bb and c_p > bb_l[i]: continue
                    entry = c_p
                    stop = entry * (1.0 - stop_pct)
                    risk = entry - stop
                    if risk > 0:
                        cands.append({'t': t, 'dir': 'LONG', 'entry': entry, 'stop': stop, 'target': vw_p, 'idx': i, 'risk': risk, 'score': (rsi_os - r_val) + ((vw_p - c_p)/c_p * 100)})
                    break
                    
                # SHORT
                if r_val >= rsi_ob and (c_p - vw_p) / vw_p >= dev:
                    if bb and c_p < bb_u[i]: continue
                    entry = c_p
                    stop = entry * (1.0 + stop_pct)
                    risk = stop - entry
                    if risk > 0:
                        cands.append({'t': t, 'dir': 'SHORT', 'entry': entry, 'stop': stop, 'target': vw_p, 'idx': i, 'risk': risk, 'score': (r_val - rsi_ob) + ((c_p - vw_p)/vw_p * 100)})
                    break
                    
        sel = sorted(cands, key=lambda x: x['score'], reverse=True)[:max_trades]
        for s in sel:
            t = s['t']
            d = d_tickers[t]
            entry, stop, target, direction, idx, risk = s['entry'], s['stop'], s['target'], s['dir'], s['idx'], s['risk']
            qty = max(1, int(RISK / risk))
            curr_stop = stop
            exit_p = None
            
            for j in range(idx + 1, d['len']):
                if j == d['len'] - 1:
                    exit_p = d['close'][j]
                    break
                if trail:
                    if direction == 'LONG' and d['high'][j] >= entry + 0.8 * risk:
                        curr_stop = max(curr_stop, entry + 0.1 * risk)
                    elif direction == 'SHORT' and d['low'][j] <= entry - 0.8 * risk:
                        curr_stop = min(curr_stop, entry - 0.1 * risk)
                        
                if direction == 'LONG':
                    if d['low'][j] <= curr_stop: exit_p = curr_stop; break
                    if d['high'][j] >= target: exit_p = target; break
                else:
                    if d['high'][j] <= curr_stop: exit_p = curr_stop; break
                    if d['low'][j] <= target: exit_p = target; break
                    
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
    
    print(f"{name}")
    print(f"  Trades: {len(trades)} | Win Rate: {wr:.1f}% | Net P&L: ₹{total_net:+,.0f} | PF: {pf:.2f}")
    print(f"  Years : " + " | ".join([f"{y}: ₹{p:+,.0f}" for y, p in sorted(yearly.items())]) + "\n", flush=True)

print("--- RESULTS MATRIX ---", flush=True)
run_sim("1. Baseline (RSI 25/75, Dev 0.8%, Stop 0.8%, Max 2)", rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=False, bb=False, max_trades=2)
run_sim("2. + Halfway Breakeven Trailing Stop", rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=True, bb=False, max_trades=2)
run_sim("3. + Bollinger Band Gating (BB Touch Required)", rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=False, bb=True, max_trades=2)
run_sim("4. + BB Gating + Breakeven Trailing Stop", rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=True, bb=True, max_trades=2)
run_sim("5. Deep Extension (Dev > 1.0%) + BB + Trail", rsi_os=25, rsi_ob=75, dev=0.010, stop_pct=0.008, trail=True, bb=True, max_trades=2)
run_sim("6. Ultra-Oversold (RSI 22/78) + BB + Trail", rsi_os=22, rsi_ob=78, dev=0.008, stop_pct=0.008, trail=True, bb=True, max_trades=2)
run_sim("7. High Conviction Single Slot (Max 1 Trade/Day, BB + Trail)", rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, trail=True, bb=True, max_trades=1)

