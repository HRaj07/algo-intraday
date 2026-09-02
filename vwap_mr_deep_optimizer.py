import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict
import itertools
import warnings
warnings.filterwarnings('ignore')

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "TITAN.NS", "SUNPHARMA.NS", "MARUTI.NS", "M&M.NS",
    "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS", "TATASTEEL.NS", "ULTRACEMCO.NS",
    "ASIANPAINT.NS", "DIVISLAB.NS", "WIPRO.NS", "HCLTECH.NS"
]

print(f"Fetching 730d (2-year) hourly dataset for {len(TICKERS)} tickers in bulk...", flush=True)
df_1h = yf.download(TICKERS, period="730d", interval="1h", auto_adjust=True, progress=False, group_by='ticker')
df_d = yf.download(TICKERS, period="3y", interval="1d", auto_adjust=True, progress=False, group_by='ticker')

data_1h = {}
data_d = {}

for t in TICKERS:
    try:
        sub_1h = df_1h[t].dropna()
        sub_1h.columns = [c.lower() for c in sub_1h.columns]
        if sub_1h.index.tz is not None: sub_1h.index = sub_1h.index.tz_localize(None)
        if len(sub_1h) > 50: data_1h[t] = sub_1h
        
        sub_d = df_d[t].dropna()
        sub_d.columns = [c.lower() for c in sub_d.columns]
        if sub_d.index.tz is not None: sub_d.index = sub_d.index.tz_localize(None)
        if len(sub_d) > 50: data_d[t] = sub_d
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
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower

def atr_calc(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()

precalc = {}
for t, df in data_1h.items():
    bb_up, bb_mid, bb_low = bb_calc(df)
    precalc[t] = {
        "vwap": vwap_hourly(df),
        "rsi": rsi_calc(df, 14),
        "rsi_9": rsi_calc(df, 9),
        "bb_up": bb_up,
        "bb_low": bb_low,
        "atr": atr_calc(df, 14),
        "vol_ma": df['volume'].rolling(20).mean(),
        "daily_df": data_d.get(t, pd.DataFrame()),
    }

sample_t = list(data_1h.keys())[0]
all_days = sorted(set(data_1h[sample_t].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

print(f"Dataset ready: {len(data_1h)} tickers across {len(all_days)} trading days (730d range).\n", flush=True)

def evaluate_mr_engine(
    rsi_period=14,
    rsi_os=25,
    rsi_ob=75,
    min_vwap_dev=0.008,
    bb_filter=False,
    vol_surge_filter=False,
    vol_mult=1.2,
    exit_mode="vwap", # "vwap", "vwap_cross", "rr_fixed", "trailing"
    stop_mode="pct",  # "pct", "atr"
    stop_val=0.008,   # 0.008 = 0.8% or 1.5 ATR
    rr_target=1.5,
    time_window_start=1, # 1 = after 10:15
    time_window_end=5,   # 5 = before 14:15
    max_daily_trades=2,
    trend_filter=None,   # None, "sma50_daily", "macro_vwap"
):
    trades = []
    daily_pnl = defaultdict(float)
    yearly_pnl = defaultdict(float)

    for day in all_days:
        candidates = []
        for t, df in data_1h.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            vw = precalc[t]['vwap']
            rsi_series = precalc[t]['rsi'] if rsi_period == 14 else precalc[t]['rsi_9']
            bb_u = precalc[t]['bb_up']
            bb_l = precalc[t]['bb_low']
            atr_series = precalc[t]['atr']
            vol_ma = precalc[t]['vol_ma']
            
            # Trend filter checks
            if trend_filter == "sma50_daily":
                df_d = precalc[t]['daily_df']
                past_d = df_d.loc[:pd.Timestamp(day)]
                if len(past_d) >= 55:
                    sma50_d = past_d['close'].iloc[:-1].rolling(50).mean().iloc[-1]
                    prev_close_d = past_d.iloc[-2]['close']
                    daily_bull = prev_close_d > sma50_d
                    daily_bear = prev_close_d < sma50_d
                else:
                    daily_bull, daily_bear = True, True
            else:
                daily_bull, daily_bear = True, True

            for i in range(time_window_start, min(time_window_end + 1, len(today) - 1)):
                bar = today.iloc[i]
                vw_val = vw.loc[today.index[i]]
                rsi_val = rsi_series.loc[today.index[i]]
                atr_val = atr_series.loc[today.index[i]]
                b_low_val = bb_l.loc[today.index[i]]
                b_up_val = bb_u.loc[today.index[i]]
                v_ma = vol_ma.loc[today.index[i]]
                
                vol_ok = True
                if vol_surge_filter:
                    vol_ok = bar['volume'] >= vol_mult * v_ma

                # LONG: Oversold Extension
                dev_long = (vw_val - bar['close']) / bar['close']
                if rsi_val <= rsi_os and dev_long >= min_vwap_dev and vol_ok:
                    if bb_filter and bar['close'] > b_low_val: continue
                    if trend_filter == "sma50_daily" and not daily_bull: continue
                    
                    entry = bar['close']
                    if stop_mode == "pct":
                        stop = entry * (1.0 - stop_val)
                    else: # atr
                        stop = entry - stop_val * atr_val
                    risk = entry - stop
                    
                    if risk > 0 and risk / entry < 0.025:
                        if exit_mode == "rr_fixed":
                            target = entry + rr_target * risk
                        else:
                            target = vw_val
                            
                        score = (rsi_os - rsi_val) * 2 + (dev_long * 100)
                        candidates.append({
                            'ticker': t, 'dir': 'LONG', 'entry': entry, 'stop': stop,
                            'target': target, 'bar_idx': i, 'risk': risk, 'score': score
                        })
                    break
                    
                # SHORT: Overbought Extension
                dev_short = (bar['close'] - vw_val) / vw_val
                if rsi_val >= rsi_ob and dev_short >= min_vwap_dev and vol_ok:
                    if bb_filter and bar['close'] < b_up_val: continue
                    if trend_filter == "sma50_daily" and not daily_bear: continue
                    
                    entry = bar['close']
                    if stop_mode == "pct":
                        stop = entry * (1.0 + stop_val)
                    else:
                        stop = entry + stop_val * atr_val
                    risk = stop - entry
                    
                    if risk > 0 and risk / entry < 0.025:
                        if exit_mode == "rr_fixed":
                            target = entry - rr_target * risk
                        else:
                            target = vw_val
                            
                        score = (rsi_val - rsi_ob) * 2 + (dev_short * 100)
                        candidates.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': entry, 'stop': stop,
                            'target': target, 'bar_idx': i, 'risk': risk, 'score': score
                        })
                    break

        selected = sorted(candidates, key=lambda x: x['score'], reverse=True)[:max_daily_trades]
        for sig in selected:
            t = sig['ticker']
            df = data_1h[t]
            today = df[df.index.date == day]
            entry, stop, target, direction, bar_idx, risk = sig['entry'], sig['stop'], sig['target'], sig['dir'], sig['bar_idx'], sig['risk']
            qty = max(1, int(RISK_PER_TRADE / risk))
            curr_stop = stop
            exit_p = None
            result = "squareoff"
            
            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_p = bar['close']
                    result = "squareoff"
                    break
                    
                # Breakeven trail if price reaches halfway to VWAP
                if exit_mode == "trailing":
                    if direction == "LONG" and bar['high'] >= entry + 0.8 * risk:
                        curr_stop = max(curr_stop, entry + 0.1 * risk)
                    elif direction == "SHORT" and bar['low'] <= entry - 0.8 * risk:
                        curr_stop = min(curr_stop, entry - 0.1 * risk)

                if direction == 'LONG':
                    if bar['low'] <= curr_stop:
                        exit_p = curr_stop
                        result = "stop"
                        break
                    if bar['high'] >= target:
                        exit_p = target
                        result = "target"
                        break
                else:
                    if bar['high'] >= curr_stop:
                        exit_p = curr_stop
                        result = "stop"
                        break
                    if bar['low'] <= target:
                        exit_p = target
                        result = "target"
                        break
                        
            if exit_p is None: continue
            pnl = (exit_p - entry) * qty if direction == 'LONG' else (entry - exit_p) * qty
            net_pnl = pnl - COST_PER_TRADE
            trades.append(net_pnl)
            daily_pnl[day] += net_pnl
            yearly_pnl[day.year] += net_pnl

    wins = [t for t in trades if t > 0]
    total_net = sum(trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_win = sum(wins)
    gross_loss = abs(sum(t for t in trades if t <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99
    green_days = sum(1 for v in daily_pnl.values() if v > 0)
    
    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "pf": pf,
        "net_pnl": total_net,
        "green_days": green_days,
        "yearly": dict(yearly_pnl)
    }

print("Running parameter grid across 120+ combinations...", flush=True)

results = []

# Sweep parameters
rsi_thresholds = [(20, 80), (22, 78), (25, 75), (28, 72)]
vwap_devs = [0.006, 0.008, 0.010, 0.012]
stop_settings = [("pct", 0.006), ("pct", 0.008), ("pct", 0.010), ("atr", 1.2), ("atr", 1.5)]
exit_modes = ["vwap", "trailing", "rr_fixed"]
bb_options = [False, True]
max_trade_options = [1, 2]

for (os, ob) in rsi_thresholds:
    for dev in vwap_devs:
        for (sm, sv) in stop_settings:
            for em in exit_modes:
                for bb in bb_options:
                    for mt in max_trade_options:
                        res = evaluate_mr_engine(
                            rsi_os=os, rsi_ob=ob, min_vwap_dev=dev,
                            stop_mode=sm, stop_val=sv, exit_mode=em,
                            bb_filter=bb, max_daily_trades=mt
                        )
                        if res["trades"] >= 60: # statistically valid sample
                            cfg_desc = f"RSI({os}/{ob}) Dev({dev*100:.1f}%) Stop({sm}:{sv}) Exit({em}) BB({bb}) MaxT({mt})"
                            results.append((res["net_pnl"], res["pf"], res["win_rate"], res["trades"], res["yearly"], cfg_desc, res))

# Sort by Net PnL and Profit Factor
results.sort(key=lambda x: (x[0], x[1]), reverse=True)

print("\n=========================================================================================")
print("🏆 TOP 10 HIGHEST PROFIT CONFIGURATIONS (2023 - 2026)")
print("=========================================================================================\n")

for i, (pnl, pf, wr, n_trades, yr, desc, r) in enumerate(results[:10]):
    print(f"Rank #{i+1}: Net P&L: ₹{pnl:+,.0f} | PF: {pf:.2f} | Win Rate: {wr:.1f}% | Trades: {n_trades}")
    print(f"  Config: {desc}")
    yr_str = " | ".join([f"{y}: ₹{p:+,.0f}" for y, p in sorted(yr.items())])
    print(f"  Years : {yr_str}\n")

