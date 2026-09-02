import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "TITAN.NS", "SUNPHARMA.NS", "MARUTI.NS", "M&M.NS",
    "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS", "TATASTEEL.NS", "ULTRACEMCO.NS"
]

print("Loading cached data...", flush=True)
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

def atr_calc(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()

precalc = {}
for t, df in data_1h.items():
    bb_u, bb_l = bb_calc(df)
    precalc[t] = {
        "vwap": vwap_hourly(df),
        "rsi": rsi_calc(df, 14),
        "bb_up": bb_u,
        "bb_low": bb_l,
        "atr": atr_calc(df, 14),
    }

sample_t = list(data_1h.keys())[0]
all_days = sorted(set(data_1h[sample_t].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

# Pre-extract daily slices to make backtest 100x faster
day_slices = []
for day in all_days:
    d_dict = {}
    for t, df in data_1h.items():
        sub = df[df.index.date == day]
        if len(sub) >= 4:
            d_dict[t] = {
                "df": sub,
                "vwap": precalc[t]["vwap"].loc[sub.index],
                "rsi": precalc[t]["rsi"].loc[sub.index],
                "bb_u": precalc[t]["bb_up"].loc[sub.index],
                "bb_l": precalc[t]["bb_low"].loc[sub.index],
                "atr": precalc[t]["atr"].loc[sub.index],
            }
    day_slices.append((day, d_dict))

print(f"Pre-processed {len(day_slices)} trading days. Running ultra-fast grid...", flush=True)

def eval_config(rsi_os, rsi_ob, dev, stop_val, exit_mode, bb_filter, max_trades):
    trades = []
    yearly_pnl = defaultdict(float)

    for day, d_dict in day_slices:
        candidates = []
        for t, data in d_dict.items():
            today = data["df"]
            vw = data["vwap"]
            rsi = data["rsi"]
            bb_u = data["bb_u"]
            bb_l = data["bb_l"]
            atr = data["atr"]

            for i in range(1, len(today) - 1):
                idx = today.index[i]
                bar = today.iloc[i]
                vw_val = vw.loc[idx]
                rsi_val = rsi.loc[idx]
                atr_val = atr.loc[idx]

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
            today = d_dict[t]["df"]
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
    return total_net, pf, win_rate, len(trades), dict(yearly_pnl)

# Run full grid
results = []
for os, ob in [(20, 80), (23, 77), (25, 75), (28, 72)]:
    for dev in [0.006, 0.008, 0.010, 0.012]:
        for stop_val in [0.006, 0.008, 0.010, 0.012]:
            for exit_mode in ["vwap", "trail", "rr15"]:
                for bb in [False, True]:
                    for mt in [1, 2]:
                        pnl, pf, wr, n_tr, yr = eval_config(os, ob, dev, stop_val, exit_mode, bb, mt)
                        if n_tr >= 50:
                            cfg = f"RSI({os}/{ob}) Dev({dev*100:.1f}%) Stop({stop_val*100:.1f}%) Exit({exit_mode}) BB({bb}) MaxT({mt})"
                            results.append((pnl, pf, wr, n_tr, yr, cfg))

results.sort(key=lambda x: (x[0], x[1]), reverse=True)

print("\n=========================================================================================")
print("🏆 TOP 10 HIGHEST PROFIT CONFIGURATIONS (2023 - 2026)")
print("=========================================================================================\n")

for i, (pnl, pf, wr, n_trades, yr, desc) in enumerate(results[:10]):
    print(f"Rank #{i+1}: Net P&L: ₹{pnl:+,.0f} | PF: {pf:.2f} | Win Rate: {wr:.1f}% | Trades: {n_trades}")
    print(f"  Config: {desc}")
    yr_str = " | ".join([f"{y}: ₹{p:+,.0f}" for y, p in sorted(yr.items())])
    print(f"  Years : {yr_str}\n")
