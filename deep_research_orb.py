import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "TATAMOTORS.NS", "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS"
]

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

def vwap(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

def atr(df, period=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"]-df["low"], (df["high"]-pc).abs(), (df["low"]-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period-1, adjust=False).mean()

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

# Precalculate indicators
precalc = {}
for t, df in data.items():
    precalc[t] = {
        "vwap": vwap(df),
        "atr": atr(df),
        "adx": adx(df),
        "ema20": df['close'].ewm(span=20, adjust=False).mean(),
        "ema50": df['close'].ewm(span=50, adjust=False).mean(),
        "ema200": df['close'].ewm(span=200, adjust=False).mean(),
    }

all_days = sorted(set(list(data.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

# Let's test combinations of 30-min ORB, Breakout time windows, Target ratios, and Trailing Stops
def run_orb30_grid(rr_ratio=2.0, sl_mode="range_opposite", min_adx=15, max_trades=2, use_trail=False):
    trades = []
    daily_pnl = defaultdict(float)
    
    for day in all_days:
        day_signals = []
        for t, df in data.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            r30_hi = max(today.iloc[0]['high'], today.iloc[1]['high'])
            r30_lo = min(today.iloc[0]['low'], today.iloc[1]['low'])
            rng = r30_hi - r30_lo
            c_open = today.iloc[1]['close']
            if rng / c_open > 0.03 or rng / c_open < 0.003: continue
            
            adx_val = precalc[t]['adx'].loc[today.index[1]] if today.index[1] in precalc[t]['adx'].index else 0
            if adx_val < min_adx: continue
            
            vw = precalc[t]['vwap']
            at = precalc[t]['atr'].loc[today.index[1]]
            
            # Search breakout from bar 2 (9:45) to bar 8 (11:15)
            for i in range(2, min(9, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                
                # LONG
                if bar['close'] > r30_hi and prev['close'] <= r30_hi and bar['close'] > vw_val:
                    if sl_mode == "range_opposite":
                        stop = r30_lo
                    elif sl_mode == "mid_range":
                        stop = (r30_hi + r30_lo) / 2
                    elif sl_mode == "atr":
                        stop = bar['close'] - 1.5 * at
                    else:
                        stop = r30_lo
                        
                    risk = bar['close'] - stop
                    if risk > 0:
                        target = bar['close'] + rr_ratio * risk
                        day_signals.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break
                    
                # SHORT
                if bar['close'] < r30_lo and prev['close'] >= r30_lo and bar['close'] < vw_val:
                    if sl_mode == "range_opposite":
                        stop = r30_hi
                    elif sl_mode == "mid_range":
                        stop = (r30_hi + r30_lo) / 2
                    elif sl_mode == "atr":
                        stop = bar['close'] + 1.5 * at
                    else:
                        stop = r30_hi
                        
                    risk = stop - bar['close']
                    if risk > 0:
                        target = bar['close'] - rr_ratio * risk
                        day_signals.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break

        day_signals = sorted(day_signals, key=lambda x: x['score'], reverse=True)[:max_trades]
        
        for sig in day_signals:
            t = sig['ticker']
            df = data[t]
            today = df[df.index.date == day]
            entry = sig['entry']
            stop = sig['stop']
            target = sig['target']
            direction = sig['dir']
            bar_idx = sig['bar_idx']
            risk = sig['risk']
            qty = max(1, int(RISK_PER_TRADE / risk))
            
            curr_stop = stop
            exit_price = None
            result = "squareoff"
            
            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_price = bar['close']
                    result = 'squareoff'
                    break
                
                if use_trail:
                    # Move stop to breakeven if 1R reached
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
            trades.append({"net_pnl": net_pnl, "result": result})
            daily_pnl[day] += net_pnl
            
    if not trades: return None
    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99
    
    return {
        "trades": len(trades), "win_rate": win_rate, "net_pnl": total_net,
        "pf": pf, "profit_days": profit_days, "loss_days": loss_days
    }

print("Running Grid Search on 30-min ORB + Supertrend / Trailing / R:R parameters...")
configs = [
    ("SL: Opposite, RR: 1:1.5, ADX>15, MaxT: 1, Trail: No", {"sl_mode": "range_opposite", "rr_ratio": 1.5, "min_adx": 15, "max_trades": 1, "use_trail": False}),
    ("SL: Opposite, RR: 1:2.0, ADX>15, MaxT: 1, Trail: No", {"sl_mode": "range_opposite", "rr_ratio": 2.0, "min_adx": 15, "max_trades": 1, "use_trail": False}),
    ("SL: Opposite, RR: 1:2.0, ADX>18, MaxT: 1, Trail: Yes", {"sl_mode": "range_opposite", "rr_ratio": 2.0, "min_adx": 18, "max_trades": 1, "use_trail": True}),
    ("SL: Mid-Range, RR: 1:2.0, ADX>15, MaxT: 1, Trail: No", {"sl_mode": "mid_range", "rr_ratio": 2.0, "min_adx": 15, "max_trades": 1, "use_trail": False}),
    ("SL: Mid-Range, RR: 1:2.5, ADX>18, MaxT: 1, Trail: Yes", {"sl_mode": "mid_range", "rr_ratio": 2.5, "min_adx": 18, "max_trades": 1, "use_trail": True}),
    ("SL: ATR 1.5x, RR: 1:2.0, ADX>18, MaxT: 1, Trail: Yes", {"sl_mode": "atr", "rr_ratio": 2.0, "min_adx": 18, "max_trades": 1, "use_trail": True}),
    ("SL: Opposite, RR: 1:2.0, ADX>20, MaxT: 2, Trail: Yes", {"sl_mode": "range_opposite", "rr_ratio": 2.0, "min_adx": 20, "max_trades": 2, "use_trail": True}),
]

for name, cfg in configs:
    res = run_orb30_grid(**cfg)
    if res:
        print(f"[{name}]")
        print(f"  Trades: {res['trades']} | Win Rate: {res['win_rate']:.1f}% | Net P&L: ₹{res['net_pnl']:+,.0f} | PF: {res['pf']:.2f} | Profit Days: {res['profit_days']}/{len(all_days)} | Loss Days: {res['loss_days']}")
