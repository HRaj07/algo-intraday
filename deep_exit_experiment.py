import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS", "TATASTEEL.NS",
    "M&M.NS", "ASIANPAINT.NS", "ULTRACEMCO.NS", "DIVISLAB.NS"
]

data_15m = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty and len(df) > 50:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None: df.index = df.index.tz_localize(None)
            data_15m[t] = df.dropna()
    except Exception: pass

def vwap_15m(df):
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
for t, df in data_15m.items():
    precalc[t] = {
        "vwap": vwap_15m(df),
        "adx": adx_calc(df),
    }

all_days = sorted(set(list(data_15m.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

def test_exits(name, rr=2.0, trail_type="none", trail_step=1.0, trail_buf=0.1, partial_tp=False):
    trades = []
    daily_pnl = defaultdict(float)

    for day in all_days:
        candidates = []
        for t, df in data_15m.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            bar0, bar1 = today.iloc[0], today.iloc[1]
            r_hi = max(bar0['high'], bar1['high'])
            r_lo = min(bar0['low'], bar1['low'])
            rng = r_hi - r_lo
            c_open = bar1['close']
            
            if rng / c_open > 0.028 or rng / c_open < 0.003: continue
            
            adx_val = precalc[t]['adx'].loc[today.index[0]] if today.index[0] in precalc[t]['adx'].index else 0
            if adx_val < 18: continue
            
            vw = precalc[t]['vwap']
            
            for i in range(2, min(9, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                
                # LONG
                if bar['close'] > r_hi and prev['close'] <= r_hi and bar['close'] > vw_val:
                    stop = r_lo
                    risk = bar['close'] - stop
                    if risk > 0 and risk / bar['close'] < 0.018:
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
                    if risk > 0 and risk / bar['close'] < 0.018:
                        target = bar['close'] - rr * risk
                        candidates.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break

        selected = sorted(candidates, key=lambda x: x['score'], reverse=True)[:2]
        
        for sig in selected:
            t = sig['ticker']
            df = data_15m[t]
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
                    
                if trail_type == "be":
                    if direction == 'LONG' and bar['high'] >= entry + trail_step * risk:
                        curr_stop = max(curr_stop, entry + trail_buf * risk)
                    elif direction == 'SHORT' and bar['low'] <= entry - trail_step * risk:
                        curr_stop = min(curr_stop, entry - trail_buf * risk)
                elif trail_type == "dynamic":
                    # Trail 1R behind max high
                    if direction == 'LONG':
                        curr_stop = max(curr_stop, bar['high'] - 1.2 * risk)
                    else:
                        curr_stop = min(curr_stop, bar['low'] + 1.2 * risk)
                    
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

    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99

    print(f"[{name}]")
    print(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f} | PF: {pf:.2f} | Green Days: {profit_days}/{len(all_days)} ({profit_days/len(all_days)*100:.1f}%)\n")

test_exits("1. Fixed Target RR 2.0 (No Trailing Stop)", rr=2.0, trail_type="none")
test_exits("2. Breakeven at +1.0R (Buf 0.1R) + Target RR 2.0", rr=2.0, trail_type="be", trail_step=1.0, trail_buf=0.1)
test_exits("3. Fixed Target RR 2.5 (No Trailing Stop)", rr=2.5, trail_type="none")
test_exits("4. Fixed Target RR 1.5 (No Trailing Stop)", rr=1.5, trail_type="none")
test_exits("5. Dynamic Trailing (1.2R Trail Behind High)", rr=3.0, trail_type="dynamic")
