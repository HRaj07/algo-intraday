import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "TITAN.NS", "SUNPHARMA.NS", "MARUTI.NS", "M&M.NS"
]

print("Downloading daily 3-year data for 15 liquid tickers...", flush=True)
df_d = yf.download(TICKERS, period="3y", interval="1d", auto_adjust=True, progress=False, group_by='ticker')

data = {}
for t in TICKERS:
    try:
        sub = df_d[t].dropna()
        sub.columns = [c.lower() for c in sub.columns]
        if sub.index.tz is not None: sub.index = sub.index.tz_localize(None)
        if len(sub) > 100: data[t] = sub
    except Exception: pass

COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

sample_t = list(data.keys())[0]
all_days = sorted(set(data[sample_t].index.date))[55:]

print(f"Testing over {len(all_days)} trading days (2023-2026)...", flush=True)

def run_daily_model(name, require_sma50=True, require_narrow_cpr=True, cpr_thresh=0.0035, max_trades=1):
    trades = []
    yearly_pnl = defaultdict(float)
    
    for day in all_days:
        candidates = []
        for t, df in data.items():
            past = df.loc[:pd.Timestamp(day)]
            if len(past) < 55: continue
            
            today_bar = past.iloc[-1]
            if today_bar.name.date() != day: continue
            
            y_bar = past.iloc[-2]
            
            sma50 = past['close'].iloc[:-1].rolling(50).mean().iloc[-1]
            is_bull = y_bar['close'] > sma50
            is_bear = y_bar['close'] < sma50
            
            yh, yl, yc = y_bar['high'], y_bar['low'], y_bar['close']
            pivot = (yh + yl + yc) / 3
            bc = (yh + yl) / 2
            tc = (pivot - bc) + pivot
            cpr_width = abs(tc - bc) / pivot
            
            if require_narrow_cpr and cpr_width > cpr_thresh:
                continue
                
            open_p, high_p, low_p, close_p = today_bar['open'], today_bar['high'], today_bar['low'], today_bar['close']
            
            # LONG
            if is_bull and high_p > yh and open_p < yh:
                entry = yh
                stop = max(yl, entry * 0.985)
                risk = entry - stop
                if risk > entry * 0.002: # at least 0.2% risk
                    target = entry + 2.0 * risk
                    if low_p <= stop: exit_p = stop
                    elif high_p >= target: exit_p = target
                    else: exit_p = close_p
                        
                    qty = max(1, int(RISK_PER_TRADE / risk))
                    pnl = (exit_p - entry) * qty - COST_PER_TRADE
                    score = (entry - sma50) / sma50 - (cpr_width * 10)
                    candidates.append({'ticker': t, 'pnl': pnl, 'score': score, 'dir': 'LONG'})
                
            # SHORT
            elif not require_sma50 or is_bear:
                if low_p < yl and open_p > yl:
                    entry = yl
                    stop = min(yh, entry * 1.015)
                    risk = stop - entry
                    if risk > entry * 0.002: # at least 0.2% risk
                        target = entry - 2.0 * risk
                        if high_p >= stop: exit_p = stop
                        elif low_p <= target: exit_p = target
                        else: exit_p = close_p
                            
                        qty = max(1, int(RISK_PER_TRADE / risk))
                        pnl = (entry - exit_p) * qty - COST_PER_TRADE
                        score = (sma50 - entry) / sma50 - (cpr_width * 10)
                        candidates.append({'ticker': t, 'pnl': pnl, 'score': score, 'dir': 'SHORT'})

        selected = sorted(candidates, key=lambda x: x['score'], reverse=True)[:max_trades]
        for c in selected:
            trades.append(c)
            yearly_pnl[day.year] += c['pnl']

    wins = [t for t in trades if t['pnl'] > 0]
    total_net = sum(t['pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_win = sum(t['pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['pnl'] <= 0)) if 'net_pnl' in trades[0] else abs(sum(t['pnl'] for t in trades if t['pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99

    print(f"\n==================================================", flush=True)
    print(f"  {name}", flush=True)
    print(f"==================================================", flush=True)
    print(f"  Total Trades     : {len(trades)}", flush=True)
    print(f"  Win Rate         : {win_rate:.1f}%", flush=True)
    print(f"  Profit Factor    : {pf:.2f}", flush=True)
    print(f"  Net P&L          : ₹{total_net:+,.0f}", flush=True)
    print(f"  Yearly Breakdown :", flush=True)
    for y, p in sorted(yearly_pnl.items()):
        print(f"    Year {y} : ₹{p:+10,.0f}", flush=True)
    print(f"==================================================\n", flush=True)

run_daily_model("1. All CPR Breakouts (No Trend Filter, Max 2)", require_sma50=False, require_narrow_cpr=False, max_trades=2)
run_daily_model("2. Trend Filter (50 SMA) + Narrow CPR (<0.35%, Max 1)", require_sma50=True, require_narrow_cpr=True, cpr_thresh=0.0035, max_trades=1)
run_daily_model("3. Trend Filter (50 SMA) + Ultra-Narrow CPR (<0.20%, Max 1)", require_sma50=True, require_narrow_cpr=True, cpr_thresh=0.0020, max_trades=1)
