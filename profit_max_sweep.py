import pickle
import pandas as pd
import numpy as np
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

with open("/Users/harshit/Documents/StreamFab/algo-intraday/cache_730d.pkl", "rb") as f:
    data_1h = pickle.load(f)

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
        "rsi": rsi_calc(df, 14),
    }

sample_t = list(data_1h.keys())[0]
all_days = sorted(set(data_1h[sample_t].index.date))
COST = 50
RISK = 2000

def test_profit_engine(name, rsi_os, rsi_ob, dev, stop_pct, target_mult=1.0, max_trades=2, time_window=1):
    trades = []
    yearly = defaultdict(float)

    for day in all_days:
        cands = []
        for t, df in data_1h.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            vw = precalc[t]['vwap'].loc[today.index]
            rsi = precalc[t]['rsi'].loc[today.index]

            for i in range(time_window, len(today) - 1):
                c_p = today.iloc[i]['close']
                vw_p = vw.iloc[i]
                r_val = rsi.iloc[i]
                
                # LONG: Buy dip below VWAP
                if r_val <= rsi_os and (vw_p - c_p) / c_p >= dev:
                    entry = c_p
                    stop = entry * (1.0 - stop_pct)
                    risk = entry - stop
                    if risk > 0:
                        # Target can be VWAP or slightly beyond VWAP (e.g. 1.0x to 1.1x VWAP distance)
                        target = entry + target_mult * (vw_p - entry)
                        cands.append({'t': t, 'dir': 'LONG', 'entry': entry, 'stop': stop, 'target': target, 'idx': i, 'risk': risk, 'score': (rsi_os - r_val) + ((vw_p - c_p)/c_p * 100)})
                    break
                    
                # SHORT: Short rip above VWAP
                if r_val >= rsi_ob and (c_p - vw_p) / vw_p >= dev:
                    entry = c_p
                    stop = entry * (1.0 + stop_pct)
                    risk = stop - entry
                    if risk > 0:
                        target = entry - target_mult * (entry - vw_p)
                        cands.append({'t': t, 'dir': 'SHORT', 'entry': entry, 'stop': stop, 'target': target, 'idx': i, 'risk': risk, 'score': (r_val - rsi_ob) + ((c_p - vw_p)/vw_p * 100)})
                    break

        sel = sorted(cands, key=lambda x: x['score'], reverse=True)[:max_trades]
        for s in sel:
            t = s['t']
            today = data_1h[t][data_1h[t].index.date == day]
            entry, stop, target, direction, idx, risk = s['entry'], s['stop'], s['target'], s['dir'], s['idx'], s['risk']
            qty = max(1, int(RISK / risk))
            exit_p = None
            
            for j in range(idx + 1, len(today)):
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

test_profit_engine("1. Baseline (RSI 25/75, Dev 0.8%, Stop 0.8%, VWAP target, Max 2)",
                   rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.008, target_mult=1.0, max_trades=2)

test_profit_engine("2. Asymmetric Stop: Stop 0.7% (Tight Stop, Higher R:R)",
                   rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.007, target_mult=1.0, max_trades=2)

test_profit_engine("3. Asymmetric Stop: Stop 0.6% (Ultra Tight Stop, Max Capital Efficiency)",
                   rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.006, target_mult=1.0, max_trades=2)

test_profit_engine("4. Overshoot Target (1.1x VWAP target distance)",
                   rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.007, target_mult=1.1, max_trades=2)

test_profit_engine("5. Selective Time Window (Entries starting from 10:30 only)",
                   rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.007, target_mult=1.0, max_trades=2, time_window=2)

test_profit_engine("6. Portfolio Mode: Max 3 Concurrent Trades / Day (Diversified Dip Buying)",
                   rsi_os=25, rsi_ob=75, dev=0.008, stop_pct=0.007, target_mult=1.0, max_trades=3, time_window=1)

