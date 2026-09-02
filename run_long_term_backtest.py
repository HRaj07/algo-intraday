import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict
from datetime import datetime

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS"
]

print("="*70)
print("📥 DOWNLOADING 2-YEAR (730-DAY) HOURLY INTRADAY DATASET FOR NSE UNIVERSE...")
print("="*70)

data_1h = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="730d", interval="1h", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            data_1h[t] = df.dropna()
    except Exception as e:
        pass

print(f"✅ Successfully fetched hourly dataset for {len(data_1h)} tickers.")
first_df = list(data_1h.values())[0]
print(f"Date Range: {first_df.index[0].date()} to {first_df.index[-1].date()} ({len(first_df)} hourly bars)")

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

# Precalculate indicators
precalc = {}
for t, df in data_1h.items():
    precalc[t] = {
        "vwap": vwap_hourly(df),
        "adx": adx_calc(df),
    }

all_days = sorted(set(list(data_1h.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000
INITIAL_CAPITAL = 500000

print(f"\n🚀 Running 2-Year Opening Range Breakout Simulation across {len(all_days)} trading days...")

def simulate_long_term_orb(max_daily_trades=2, min_adx=15, rr=2.0, trail_buffer=0.1):
    trades = []
    daily_pnl = defaultdict(float)
    yearly_pnl = defaultdict(float)
    monthly_pnl = defaultdict(float)

    for day in all_days:
        day_signals = []
        for t, df in data_1h.items():
            today = df[df.index.date == day]
            # Each trading day typically has ~6-7 hourly bars (9:15-10:15, 10:15-11:15, etc.)
            if len(today) < 3: continue
            
            # Opening Range: First 1-hour bar (9:15 - 10:15 AM)
            r_hi = today.iloc[0]['high']
            r_lo = today.iloc[0]['low']
            rng = r_hi - r_lo
            c_open = today.iloc[0]['close']
            if rng / c_open > 0.035 or rng / c_open < 0.003: continue
            
            adx_val = precalc[t]['adx'].loc[today.index[0]] if today.index[0] in precalc[t]['adx'].index else 0
            if adx_val < min_adx: continue
            
            vw = precalc[t]['vwap']
            
            # Scan bars from bar 1 (10:15-11:15) to bar 4 (1:15-2:15)
            for i in range(1, min(4, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                
                # LONG
                if bar['close'] > r_hi and prev['close'] <= r_hi and bar['close'] > vw_val:
                    stop = r_lo
                    risk = bar['close'] - stop
                    if risk > 0:
                        target = bar['close'] + rr * risk
                        day_signals.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break
                    
                # SHORT
                if bar['close'] < r_lo and prev['close'] >= r_lo and bar['close'] < vw_val:
                    stop = r_hi
                    risk = stop - bar['close']
                    if risk > 0:
                        target = bar['close'] - rr * risk
                        day_signals.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break

        selected = sorted(day_signals, key=lambda x: x['score'], reverse=True)[:max_daily_trades]
        
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
                    
                # Breakeven trailing stop at +1R
                if direction == 'LONG' and bar['high'] >= entry + risk:
                    curr_stop = max(curr_stop, entry + trail_buffer * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - risk:
                    curr_stop = min(curr_stop, entry - trail_buffer * risk)
                    
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
            monthly_pnl[day.strftime('%Y-%m')] += net_pnl

    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    flat_days = len(all_days) - profit_days - loss_days
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99

    print("\n" + "═"*70)
    print("  🏆 2-YEAR HISTORICAL INTRADAY BACKTEST RESULTS (Aug 2023 – Aug 2026)")
    print("═"*70)
    print(f"  Initial Capital      : ₹{INITIAL_CAPITAL:>10,.0f}")
    print(f"  Final Capital        : ₹{INITIAL_CAPITAL + total_net:>10,.0f}")
    print(f"  Net P&L              : ₹{total_net:>+10,.0f}")
    print(f"  Total Return         : {total_net/INITIAL_CAPITAL*100:>+9.2f}%")
    print("─"*70)
    print(f"  Total Trades         : {len(trades):>10}  (Avg {len(trades)/len(all_days):.2f} trades/day)")
    print(f"  Winning Trades       : {len(wins):>10}  ({win_rate:.1f}%)")
    print(f"  Losing Trades        : {len(trades)-len(wins):>10}  ({100-win_rate:.1f}%)")
    print(f"  Avg Win              : ₹{gross_win/len(wins):>+9,.0f}")
    print(f"  Avg Loss             : ₹{gross_loss/(len(trades)-len(wins)):>+9,.0f}")
    print(f"  Profit Factor        : {pf:>10.2f}")
    print("─"*70)
    print(f"  Trading Days         : {len(all_days):>10}")
    print(f"  Profitable Days      : {profit_days:>10}  ({profit_days/len(all_days)*100:.1f}%)")
    print(f"  Losing Days          : {loss_days:>10}  ({loss_days/len(all_days)*100:.1f}%)")
    print(f"  Flat / No-Trade Days : {flat_days:>10}  ({flat_days/len(all_days)*100:.1f}%)")
    print("═"*70)

    print("\n  📅 Annual Breakdown:")
    for yr, p in sorted(yearly_pnl.items()):
        print(f"  Year {yr} : ₹{p:>+10,.0f}")
    print("═"*70)
    
    return {
        "trades": len(trades), "win_rate": win_rate, "net_pnl": total_net, "pf": pf,
        "profit_days": profit_days, "loss_days": loss_days, "flat_days": flat_days,
        "yearly": dict(yearly_pnl), "monthly": dict(monthly_pnl)
    }

simulate_long_term_orb()
