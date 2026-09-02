import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS"
]

print("Downloading dataset including Nifty index...")
data = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            data[t] = df.dropna()
    except Exception as e:
        pass

# Download Nifty Index
nifty_df = yf.download("^NSEI", period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
nifty_df.columns = [c.lower() for c in nifty_df.columns]
if nifty_df.index.tz is not None:
    nifty_df.index = nifty_df.index.tz_localize(None)

def vwap(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

def adx(df, period=14):
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
for t, df in data.items():
    precalc[t] = {
        "vwap": vwap(df),
        "adx": adx(df),
    }

nifty_vwap = vwap(nifty_df)
all_days = sorted(set(list(data.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

def test_full_orb(use_index_filter=True, max_t=2, rr=2.0, min_adx=20):
    trades = []
    daily_pnl = defaultdict(float)
    
    for day in all_days:
        sigs = []
        n_day = nifty_df[nifty_df.index.date == day]
        
        for t, df in data.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            r30_hi = max(today.iloc[0]['high'], today.iloc[1]['high'])
            r30_lo = min(today.iloc[0]['low'], today.iloc[1]['low'])
            rng = r30_hi - r30_lo
            c_open = today.iloc[1]['close']
            if rng / c_open > 0.025 or rng / c_open < 0.003: continue
            
            adx_val = precalc[t]['adx'].loc[today.index[1]] if today.index[1] in precalc[t]['adx'].index else 0
            if adx_val < min_adx: continue
            
            vw = precalc[t]['vwap']
            
            for i in range(2, min(8, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                
                # Check Nifty trend alignment
                n_align_long = True
                n_align_short = True
                if use_index_filter and not n_day.empty and today.index[i] in n_day.index:
                    n_bar = n_day.loc[today.index[i]]
                    n_vw = nifty_vwap.loc[today.index[i]]
                    n_align_long = n_bar['close'] >= n_vw
                    n_align_short = n_bar['close'] <= n_vw
                
                # LONG
                if bar['close'] > r30_hi and prev['close'] <= r30_hi and bar['close'] > vw_val and n_align_long:
                    stop = r30_lo
                    risk = bar['close'] - stop
                    if risk > 0:
                        target = bar['close'] + rr * risk
                        sigs.append({'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk})
                    break
                    
                # SHORT
                if bar['close'] < r30_lo and prev['close'] >= r30_lo and bar['close'] < vw_val and n_align_short:
                    stop = r30_hi
                    risk = stop - bar['close']
                    if risk > 0:
                        target = bar['close'] - rr * risk
                        sigs.append({'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk})
                    break
                    
        sigs = sorted(sigs, key=lambda x: x['score'], reverse=True)[:max_t]
        
        for sig in sigs:
            t = sig['ticker']
            df = data[t]
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
                # Trailing stop to breakeven + 0.1R when price touches +1R
                if direction == 'LONG' and bar['high'] >= entry + risk:
                    curr_stop = max(curr_stop, entry + 0.1 * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - risk:
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
            
    if not trades: return
    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    flat_days = len(all_days) - profit_days - loss_days
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99
    
    print("\n" + "="*65)
    print(f"📊 30-MIN ORB + NIFTY TREND FILTER (IndexAlign={use_index_filter})")
    print("="*65)
    print(f"Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f}")
    print(f"Profit Factor: {pf:.2f} | Avg Win: ₹{gross_win/len(wins):,.0f} | Avg Loss: ₹{gross_loss/(len(trades)-len(wins)):,.0f}")
    print(f"Profitable Days: {profit_days}/{len(all_days)} ({profit_days/len(all_days)*100:.1f}%) | Loss Days: {loss_days} ({loss_days/len(all_days)*100:.1f}%) | Flat: {flat_days}")
    
    # Monthly breakdown
    m_pnl = defaultdict(float)
    for d, p in daily_pnl.items():
        m_pnl[d.strftime('%b %Y')] += p
    print("\nMonthly P&L:")
    for m, p in m_pnl.items():
        print(f"  {m}: ₹{p:+,.0f}")

test_full_orb(use_index_filter=False, max_t=2, rr=2.0, min_adx=20)
test_full_orb(use_index_filter=True, max_t=2, rr=2.0, min_adx=20)
