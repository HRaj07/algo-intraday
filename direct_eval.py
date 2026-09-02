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

print("Downloading 730d 1h data...", flush=True)
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
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

def test_engine(name, rsi_os, rsi_ob, dev, stop_val, exit_mode, bb_filter, max_trades):
    trades = []
    daily_pnl = defaultdict(float)
    yearly_pnl = defaultdict(float)

    for day in all_days:
        candidates = []
        for t, df in data_1h.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            vw = precalc[t]['vwap']
            rsi = precalc[t]['rsi']
            bb_u = precalc[t]['bb_u']
            bb_l = precalc[t]['bb_l']

            for i in range(1, len(today) - 1):
                bar = today.iloc[i]
                idx = today.index[i]
                vw_val = vw.loc[idx]
                rsi_val = rsi.loc[idx]

                # LONG
                dev_l = (vw_val - bar['close']) / bar['close']
                if rsi_val <= rsi_os and dev_l >= dev:
                    if bb_filter and bar['close'] > bb_l.loc[idx]: continue
                    entry = bar['close']
                    stop = entry * (1.0 - stop_val)
                    risk = entry - stop
                    if risk > 0 and risk / entry < 0.025:
                        target = entry + 1.5 * risk if exit_mode == "rr15" else vw_val
                        candidates.append({
                            'ticker': t, 'dir': 'LONG', 'entry': entry, 'stop': stop,
                            'target': target, 'bar_idx': i, 'risk': risk,
                            'score': (rsi_os - rsi_val) * 2 + (dev_l * 100)
                        })
                    break

                # SHORT
                dev_s = (bar['close'] - vw_val) / vw_val
                if rsi_val >= rsi_ob and dev_s >= dev:
                    if bb_filter and bar['close'] < bb_u.loc[idx]: continue
                    entry = bar['close']
                    stop = entry * (1.0 + stop_val)
                    risk = stop - entry
                    if risk > 0 and risk / entry < 0.025:
                        target = entry - 1.5 * risk if exit_mode == "rr15" else vw_val
                        candidates.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': entry, 'stop': stop,
                            'target': target, 'bar_idx': i, 'risk': risk,
                            'score': (rsi_val - rsi_ob) * 2 + (dev_s * 100)
                        })
                    break

        selected = sorted(candidates, key=lambda x: x['score'], reverse=True)[:max_trades]
        for sig in selected:
            t = sig['ticker']
            today = data_1h[t][data_1h[t].index.date == day]
            entry, stop, target, direction, bar_idx, risk = sig['entry'], sig['stop'], sig['target'], sig['dir'], sig['bar_idx'], sig['risk']
            qty = max(1, int(RISK_PER_TRADE / risk))
            curr_stop = stop
            exit_p = None

            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_p = bar['close']
                    break
                if exit_mode == "trail":
                    if direction == "LONG" and bar['high'] >= entry + 0.8 * risk:
                        curr_stop = max(curr_stop, entry + 0.1 * risk)
                    elif direction == "SHORT" and bar['low'] <= entry - 0.8 * risk:
                        curr_stop = min(curr_stop, entry - 0.1 * risk)

                if direction == 'LONG':
                    if bar['low'] <= curr_stop: exit_p = curr_stop; break
                    if bar['high'] >= target: exit_p = target; break
                else:
                    if bar['high'] >= curr_stop: exit_p = curr_stop; break
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
    yr_str = " | ".join([f"{y}: ₹{p:+,.0f}" for y, p in sorted(yearly_pnl.items())])
    print(f"  Years : {yr_str}\n")

print("\n--- Testing Top Parameter Archetypes ---\n", flush=True)

test_engine("Baseline: RSI < 25 / > 75 | Dev > 0.8% | Stop 0.8% | VWAP Exit | Max 2 Trades",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_val=0.008, exit_mode="vwap", bb_filter=False, max_trades=2)

test_engine("Upgrade 1: + Trail Halfway Breakeven Stop",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_val=0.008, exit_mode="trail", bb_filter=False, max_trades=2)

test_engine("Upgrade 2: + Bollinger Band Confluence (Touch BB Upper/Lower)",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_val=0.008, exit_mode="vwap", bb_filter=True, max_trades=2)

test_engine("Upgrade 3: BB Confluence + Trail Breakeven",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_val=0.008, exit_mode="trail", bb_filter=True, max_trades=2)

test_engine("Upgrade 4: Deep Extension (Dev > 1.0%) + BB Confluence + Trail",
            rsi_os=25, rsi_ob=75, dev=0.010, stop_val=0.008, exit_mode="trail", bb_filter=True, max_trades=2)

test_engine("Upgrade 5: Deep Extension (Dev > 1.0%) + Stop 0.6% + VWAP Exit",
            rsi_os=25, rsi_ob=75, dev=0.010, stop_val=0.006, exit_mode="vwap", bb_filter=False, max_trades=2)

test_engine("Upgrade 6: Highest Conviction (Max 1 Trade/Day, BB + Trail)",
            rsi_os=25, rsi_ob=75, dev=0.008, stop_val=0.008, exit_mode="trail", bb_filter=True, max_trades=1)

