"""
Replay the REAL strategy over the last ~60 days of real 15-minute bars.

    python3 backtest_recent.py                 # run it
    python3 backtest_recent.py --names 60      # faster, fewer symbols

NO LOGIC IS DUPLICATED HERE.
----------------------------
This file imports VWAPMeanReversionV2 and PaperTraderV2 and calls them in the
same order main.py does. It contains no signal rules, no sizing arithmetic, no
cost model and no thresholds. If the strategy changes, this tests the change,
because it IS the strategy.

That property is the whole point. backtest_honest.py carried its own copy of
the thresholds, and by 2026-09-24 every one had drifted from config.py - it
would have tested rsi 30 / stop floor 0.8% / deviation 0.5% while the live
system ran 40 / 0.6% / 0.796%. v1 died of exactly this: a backtest measuring a
strategy that did not exist.

WHAT THIS IS NOT
----------------
yfinance serves at most 60 days of 15-minute bars, so there is no
train/validate/test split. One period cannot separate an edge from a
favourable two months. This answers "would it trade, and what would it cost" -
nothing more. The go-live bar in README.md is unchanged.
"""
import argparse
import json
import math
import pickle
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import INTRADAY_UNIVERSE, INDEX_TICKER, VIX_TICKER, SYSTEM, RISK
from data.fetcher import IntradayFetcher
from tzutil import IST

import engine.paper_trader_v2 as pt_mod
import strategies.vwap_mr_v2 as strat_mod
from engine.paper_trader_v2 import PaperTraderV2
from strategies.vwap_mr_v2 import VWAPMeanReversionV2

CACHE = Path("cache_recent_15m.pkl")


def get_data(days: int, names: int) -> dict:
    if CACHE.exists():
        print(f"using cached bars from {CACHE} (delete to re-fetch)")
        data = pickle.load(open(CACHE, "rb"))
    else:
        syms = INTRADAY_UNIVERSE + [INDEX_TICKER, VIX_TICKER]
        print(f"fetching {days}d of 15-minute bars for {len(syms)} symbols...")
        data = IntradayFetcher().fetch_intraday(syms, days_back=days)
        pickle.dump(data, open(CACHE, "wb"))
        print(f"cached {len(data)} symbols to {CACHE}")
    if names and names < len(INTRADAY_UNIVERSE):
        keep = set(INTRADAY_UNIVERSE[:names]) | {INDEX_TICKER, VIX_TICKER}
        data = {k: v for k, v in data.items() if k in keep}
        print(f"limited to {names} names for speed")
    return data


def replay(data: dict, capital: float, verbose: bool) -> dict:
    index_df = data.pop(INDEX_TICKER, None)
    vix_df = data.pop(VIX_TICKER, None)
    if index_df is None or index_df.empty:
        raise SystemExit(f"no {INDEX_TICKER} data - the regime gate cannot evaluate")

    names = {t: df.sort_index() for t, df in data.items() if len(df) > 60}
    print(f"replaying {len(names)} names over "
          f"{len(set(index_df.index.date))} trading days\n")

    # Position lookups so each bar is an O(1) iloc view, not a boolean mask over
    # the whole frame. This is the only concession to speed, and it changes
    # nothing about what the strategy sees.
    days = sorted(set(index_df.index.date))
    state = Path("/tmp/_bt_state.json")
    state.unlink(missing_ok=True)
    trader = PaperTraderV2(state_file=str(state))
    trader.state["cash"] = trader.state["initial_capital"] = capital
    trader.state["equity_peak"] = capital
    strat = VWAPMeanReversionV2()

    rejects = defaultdict(int)
    scans = signals_seen = 0

    for day in days:
        idx_day = index_df[index_df.index.date == day]
        if len(idx_day) < 6:
            continue
        day_names = {}
        for t, df in names.items():
            d = df[df.index.date == day]
            if len(d) >= 8:
                day_names[t] = (df, df.index.get_indexer([d.index[-1]])[0] - len(d) + 1, len(d))
        if not day_names:
            continue
        nbars = max(v[2] for v in day_names.values())
        idx_start = index_df.index.get_indexer([idx_day.index[0]])[0]

        for i in range(nbars):
            if i >= len(idx_day):
                break
            now = idx_day.index[i].to_pydatetime().replace(tzinfo=IST)

            # freeze the clock for both modules - they read now_ist()
            pt_mod.now_ist = lambda n=now: n
            strat_mod.now_ist = lambda n=now: n

            sliced = {t: df.iloc[:start + i + 1]
                      for t, (df, start, n) in day_names.items() if i < n}
            idx_sliced = index_df.iloc[:idx_start + i + 1]
            vix = None
            if vix_df is not None and not vix_df.empty:
                v = vix_df[vix_df.index <= idx_day.index[i]]
                if not v.empty:
                    vix = float(v["close"].iloc[-1])

            bars = {}
            for t, df in sliced.items():
                last = df.iloc[-1]
                bars[t] = {"close": float(last["close"]), "high": float(last["high"]),
                           "low": float(last["low"]), "volume": float(last["volume"])}

            # ---- exactly main.py's order ----
            trader.update_prices(bars, now)
            trader.check_exits(bars, now)

            sq_h, sq_m = map(int, SYSTEM["square_off_time"].split(":"))
            if now.time() >= dtime(sq_h, sq_m):
                continue

            trader.process_orders(bars, now)

            halted, _ = trader.risk.trading_halted(now)
            if halted:
                rejects["HALTED (circuit breaker)"] += 1
                continue

            sigs = strat.compute_signals(sliced, idx_sliced, vix)
            scans += 1
            signals_seen += len(sigs)
            for k, v in (getattr(strat, "last_scan", {}).get("rejects") or {}).items():
                key = k.split(" -")[0].split("(")[0].strip()
                key = ("turnover" if "turnover" in key.lower() else
                       "RVOL" if "rvol" in key.lower() else
                       "RSI" if "rsi" in key.lower() else
                       "deviation" if "deviation" in key.lower() else
                       "gap" if "gap" in key.lower() else key)
                rejects[key] += v
            for s in sigs:
                trader.place_order(s, now)

        # square off whatever is left, at the day's last bar
        if trader.state["positions"]:
            eod = idx_day.index[-1].to_pydatetime().replace(tzinfo=IST).replace(hour=15, minute=10)
            pt_mod.now_ist = lambda n=eod: n
            trader.check_exits(bars, eod)

    s = trader.summary()
    h = trader.state["trade_history"]
    return {"summary": s, "trades": h, "scans": scans, "signals": signals_seen,
            "days": len(days), "rejects": dict(sorted(rejects.items(), key=lambda x: -x[1]))}


def show(r, capital):
    s, h = r["summary"], r["trades"]
    print("="*72)
    print(f"  REPLAY RESULT — {r['days']} trading days, {r['scans']} scans")
    print("="*72)
    print(f"  signals generated   {r['signals']}")
    print(f"  trades closed       {s['closed_trades']}")
    if not h:
        print("\n  NO TRADES. Where candidates died:")
        for k, v in list(r["rejects"].items())[:10]:
            print(f"    {k:<30}{v:>10,}")
        return
    pnl = [t["pnl"] for t in h]
    Rs = [t.get("R_multiple", 0) for t in h]
    sd = np.std(pnl, ddof=1) if len(pnl) > 1 else 0
    eq = pk = dd = 0
    for x in pnl:
        eq += x; pk = max(pk, eq); dd = min(dd, eq - pk)
    print(f"  win rate            {s['win_rate_pct']}%")
    print(f"  profit factor       {s['profit_factor']}")
    print(f"  net P&L             Rs{s['total_pnl']:,.0f}   ({s['return_pct']}% on Rs{capital:,.0f})")
    print(f"  friction paid       Rs{s['total_friction']:,.0f}")
    print(f"  gross before costs  Rs{s['total_pnl'] + s['total_friction']:,.0f}")
    print(f"  avg R               {np.mean(Rs):+.3f}")
    if sd:
        print(f"  t-stat              {np.mean(pnl)/(sd/math.sqrt(len(pnl))):.2f}   (>2 is the go-live bar)")
    print(f"  max drawdown        Rs{dd:,.0f}  ({abs(dd)/capital*100:.2f}%)")
    by = defaultdict(list)
    for t in h:
        by[t["reason"]].append(t["pnl"])
    print("  exits:")
    for k, v in sorted(by.items(), key=lambda x: -sum(x[1])):
        print(f"    {k:<20}{len(v):>4} trades  Rs{sum(v):>9,.0f}")
    gross = s["total_pnl"] + s["total_friction"]
    print()
    if gross > 0 and s["total_pnl"] < 0:
        print("  >> GROSS POSITIVE, NET NEGATIVE — v1's exact failure. The signal")
        print("     picks winners and cannot pay for them.")
    elif gross <= 0:
        print("  >> Negative before costs. The signal itself is not working.")
    else:
        print("  >> Net positive after real costs.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--names", type=int, default=0, help="limit symbols for speed")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    import logging
    logging.disable(logging.CRITICAL)      # the strategy logs per name per bar

    data = get_data(a.days, a.names)
    cap = SYSTEM["initial_capital"]
    r = replay(data, cap, a.verbose)
    show(r, cap)

    print("\n" + "="*72)
    print("  WHAT THIS DOES NOT TELL YOU")
    print("="*72)
    print("  60 days is ONE period. No train/validate/test split is possible, so")
    print("  this cannot separate an edge from a favourable two months. It answers")
    print("  'would it trade, and what would it cost'. Nothing more.")
    print("  Entries also assume the stop-limit filled whenever the bar traded")
    print("  through the trigger, which is optimistic on a thin bar.")


if __name__ == "__main__":
    main()
