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
df_d = yf.download(TICKERS, period="3y", interval="1d", auto_adjust=True, progress=False, group_by='ticker')
bench_d = yf.download("^NSEI", period="3y", interval="1d", auto_adjust=True, progress=False, multi_level_index=False)
bench_d.columns = [c.lower() for c in bench_d.columns]
if bench_d.index.tz is not None: bench_d.index = bench_d.index.tz_localize(None)

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

def adx_calc(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm[high.diff() <= -low.diff()] = 0
    minus_dm[-low.diff() <= high.diff()] = 0
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    a = tr.ewm(com=period - 1, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean() / a.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / a.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(com=period - 1, adjust=False).mean().fillna(0)

precalc = {}
for t, df in data_1h.items():
    precalc[t] = {
        "vwap": vwap_hourly(df),
        "adx": adx_calc(df),
        "daily_df": data_d.get(t, pd.DataFrame()),
    }

bench_sma50 = bench_d['close'].rolling(50).mean()
sample_t = list(data_1h.keys())[0]
all_days = sorted(set(data_1h[sample_t].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

def test_selective(name, only_narrow_cpr=True, min_cpr_width=0.003, min_adx=22, rr=2.0):
    trades = []
    daily_pnl = defaultdict(float)
    yearly_pnl = defaultdict(float)

    for day in all_days:
        candidates = []
        for t, df in data_1h.items():
            today = df[df.index.date == day]
            if len(today) < 3: continue
            
            df_d = precalc[t]['daily_df']
            past_d = df_d.loc[:pd.Timestamp(day)]
            if len(past_d) < 5: continue
            
            yh = past_d.iloc[-2]['high']
            yl = past_d.iloc[-2]['low']
            yc = past_d.iloc[-2]['close']
            pivot = (yh + yl + yc) / 3
            bc = (yh + yl) / 2
            tc = (pivot - bc) + pivot
            cpr_width = abs(tc - bc) / pivot
            
            # Strict filter: ONLY take trade if CPR is narrow
            if only_narrow_cpr and cpr_width > min_cpr_width:
                continue

            r_hi = today.iloc[0]['high']
            r_lo = today.iloc[0]['low']
            rng = r_hi - r_lo
            c_open = today.iloc[0]['close']
            
            if rng / c_open > 0.025 or rng / c_open < 0.004: continue
            
            adx_val = precalc[t]['adx'].loc[today.index[0]] if today.index[0] in precalc[t]['adx'].index else 0
            if adx_val < min_adx: continue
            
            vw = precalc[t]['vwap']
            
            for i in range(1, min(3, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                
                # LONG
                if bar['close'] > r_hi and prev['close'] <= r_hi and bar['close'] > vw_val:
                    stop = r_lo
                    risk = bar['close'] - stop
                    if risk > 0 and risk / bar['close'] < 0.015:
                        target = bar['close'] + rr * risk
                        candidates.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break
                    
                # SHORT
                if bar['close'] < r_lo and prev['close'] >= r_lo and bar['close'] < vw_val:
                    stop = r_hi
                    risk = stop - bar['close']
                    if risk > 0 and risk / bar['close'] < 0.015:
                        target = bar['close'] - rr * risk
                        candidates.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break

        selected = sorted(candidates, key=lambda x: x['score'], reverse=True)[:1]
        
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
                    
                if direction == 'LONG' and bar['high'] >= entry + 1.0 * risk:
                    curr_stop = max(curr_stop, entry + 0.1 * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - 1.0 * risk:
                    curr_stop = min(curr_stop, entry - 0.1 * risk)
                    
                if direction == 'LONG':
                    if bar['low'] <= curr_stop:
                        exit_price = curr_stop
                        result = 'stop'
                        break
                    if bar['high'] >= target:
                        exit_price = target
                        result = 'target'
                        break
                else:
                    if bar['high'] >= curr_stop:
                        exit_price = curr_stop
                        result = 'stop'
                        break
                    if bar['low'] <= target:
                        exit_price = target
                        result = 'target'
                        break
                        
            if exit_price is None: continue
            pnl = (exit_price - entry) * qty if direction == 'LONG' else (entry - exit_price) * qty
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

    print(f"[{name}]")
    print(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f} | PF: {pf:.2f}")
    for y, p in sorted(yearly_pnl.items()):
        print(f"    {y}: ₹{p:+,.0f}", end=" | ")
    print("\n")

test_selective("1. Only Narrow CPR (<0.25%) + ADX > 20", only_narrow_cpr=True, min_cpr_width=0.0025, min_adx=20)
test_selective("2. Only Ultra-Narrow CPR (<0.18%) + ADX > 18", only_narrow_cpr=True, min_cpr_width=0.0018, min_adx=18)
test_selective("3. Only Ultra-Narrow CPR (<0.15%) + ADX > 20", only_narrow_cpr=True, min_cpr_width=0.0015, min_adx=20)
