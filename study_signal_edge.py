"""
Does the PREMISE hold? A forward-return study of the entry condition alone.

    python3 study_signal_edge.py

WHY THIS EXISTS
---------------
backtest_recent.py replays the whole machine: filters, trigger, ATR stop,
targets, sizing, costs, circuit breakers. When it comes back negative you
cannot tell which part is at fault - and on 2026-09-24 it came back negative
BEFORE costs, which rules out the one explanation we had been working from.

This file removes the machine. It asks the narrowest possible question:

    when a stock is X% below its intraday VWAP with RSI below Y,
    what does it do next?

No stops. No targets. No position sizing. No costs. No portfolio caps. Just the
forward return from that bar, measured over several horizons, against the
unconditional return of every bar in the sample as a baseline.

If the conditional return is positive and beats the 0.0955% round-trip cost,
the premise is sound and the machine around it is what's broken. If it is flat,
there is nothing to build on and no amount of stop-tuning will help. If it is
NEGATIVE, the premise is backwards.

NOTHING HERE IS DUPLICATED
--------------------------
VWAP, RSI and ATR are imported from data.fetcher - the same objects the live
strategy calls. The thresholds are read from config.py. If you change either,
this study changes with them. The only thing written by hand is the forward
return, which the strategy does not compute.

WHAT IT CANNOT TELL YOU
-----------------------
Forward return from a bar close is not a tradeable P&L. It ignores the trigger,
so it measures the setup rather than the entry, and it ignores the stop, so a
setup that reverts +1% after first going -3% counts as a winner here and would
have been stopped out live. That asymmetry means this study is OPTIMISTIC: it
is an upper bound on what the setup can offer. A premise that fails here cannot
be rescued downstream.
"""
import sys
import pickle
from collections import defaultdict
from datetime import time as dtime

import numpy as np
import pandas as pd

from config import STRATEGY, INTRADAY_UNIVERSE, INDEX_TICKER, VIX_TICKER, SYSTEM
from costs import VARIABLE_ROUNDTRIP_PCT
from data.fetcher import TechnicalIndicators

CACHE = "cache_recent_15m.pkl"
TI = TechnicalIndicators()
HORIZONS = [1, 2, 4, 8]          # 15-minute bars: 15m, 30m, 1h, 2h


def tstat(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    return 0.0 if sd == 0 else x.mean() / (sd / np.sqrt(len(x)))


def build(data):
    """One long table: every intraday bar, its indicators, its forward returns."""
    rows = []
    first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
    last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))

    for tkr, df in data.items():
        if tkr in (INDEX_TICKER, VIX_TICKER) or len(df) < 100:
            continue
        df = df.sort_index()
        rsi = TI.rsi(df, STRATEGY["rsi_period"])
        atr = TI.atr(df, STRATEGY["atr_period"])
        vwap = TI.vwap(df)

        close = df["close"]
        high = df["high"]
        dates = pd.Series(df.index.date, index=df.index)

        # forward returns, truncated at the day boundary so nothing looks
        # through an overnight gap that an intraday system can never hold
        fwd = {}
        for h in HORIZONS:
            f = close.shift(-h) / close - 1.0
            f[dates.shift(-h) != dates] = np.nan
            fwd[h] = f
        # to the last bar of the same day (what "hold to square-off" earns)
        eod_close = close.groupby(dates.values).transform("last")
        fwd["eod"] = eod_close / close - 1.0

        # did the NEXT bar trade through this bar's high? (the v2 trigger)
        trig_px = high + STRATEGY["trigger_offset_ticks"] * 0.05
        nxt_high = high.shift(-1)
        triggered = (nxt_high >= trig_px) & (dates.shift(-1) == dates)
        # if it triggers, you are long from trig_px, not from close
        trig_fwd = {}
        for h in HORIZONS:
            f = close.shift(-h) / trig_px - 1.0
            f[dates.shift(-h) != dates] = np.nan
            trig_fwd[h] = f
        trig_fwd["eod"] = eod_close / trig_px - 1.0

        dev = (vwap - close) / close
        t = pd.Series(df.index.time, index=df.index)
        window = (t >= first) & (t <= last)
        keep = window & dev.notna() & rsi.notna() & atr.notna() & (close > 0)

        sub = pd.DataFrame({
            "ticker": tkr,
            "dev": dev,
            "rsi": rsi,
            "atr_pct": atr / close,
            "triggered": triggered,
        })[keep]
        for h in list(HORIZONS) + ["eod"]:
            sub[f"f{h}"] = fwd[h][keep]
            sub[f"g{h}"] = trig_fwd[h][keep]
        rows.append(sub)

    return pd.concat(rows, ignore_index=True)


def line(label, x, n_min=30):
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    if len(x) < n_min:
        return f"  {label:<34}{len(x):>8,}  (too few)"
    return (f"  {label:<34}{len(x):>8,}  {x.mean()*100:>+8.3f}%  "
            f"{np.median(x)*100:>+8.3f}%  {(x>0).mean()*100:>6.1f}%  {tstat(x):>+7.2f}")


HEAD = f"  {'condition':<34}{'n':>8}  {'mean':>9}  {'median':>9}  {'win%':>6}  {'t':>7}"


def main():
    data = pickle.load(open(CACHE, "rb"))
    print(f"loaded {len(data)} symbols\n")
    tab = build(data)
    print(f"{len(tab):,} bars in the entry window across {tab.ticker.nunique()} names")
    cost = VARIABLE_ROUNDTRIP_PCT
    print(f"round-trip variable cost to beat: {cost*100:.4f}%\n")

    dev_floor = STRATEGY["min_vwap_deviation"]
    rsi_max = STRATEGY["rsi_oversold"]
    setup = (tab.dev >= dev_floor) & (tab.rsi <= rsi_max)

    # ---------------------------------------------------------------- 1
    print("=" * 84)
    print("  1. BASELINE — every bar, no condition at all")
    print("=" * 84)
    print(HEAD)
    for h in list(HORIZONS) + ["eod"]:
        print(line(f"all bars, +{h} bars", tab[f"f{h}"]))

    # ---------------------------------------------------------------- 2
    print()
    print("=" * 84)
    print(f"  2. THE LIVE SETUP — dev >= {dev_floor*100:.3f}% and RSI <= {rsi_max}")
    print("     (buying the signal bar's close, v1-style, no trigger)")
    print("=" * 84)
    print(HEAD)
    for h in list(HORIZONS) + ["eod"]:
        print(line(f"setup, +{h} bars", tab.loc[setup, f"f{h}"]))
    print(f"\n  setups found: {int(setup.sum()):,} "
          f"({setup.mean()*100:.2f}% of bars)")

    # ---------------------------------------------------------------- 3
    print()
    print("=" * 84)
    print("  3. THE SAME SETUP, ENTERED ON THE v2 TRIGGER")
    print("     (long from one tick above the signal bar's high, only if hit)")
    print("=" * 84)
    print(HEAD)
    trig = setup & tab.triggered
    for h in list(HORIZONS) + ["eod"]:
        print(line(f"triggered, +{h} bars", tab.loc[trig, f"g{h}"]))
    if setup.sum():
        print(f"\n  trigger hit rate: {tab.loc[setup,'triggered'].mean()*100:.1f}% "
              f"of setups ({int(trig.sum()):,} entries)")

    # ---------------------------------------------------------------- 4
    print()
    print("=" * 84)
    print("  4. IS THE PREMISE BACKWARDS? — the mirror condition")
    print(f"     (dev <= -{dev_floor*100:.3f}%, i.e. stretched ABOVE VWAP, RSI >= {100-rsi_max})")
    print("=" * 84)
    print(HEAD)
    mirror = (tab.dev <= -dev_floor) & (tab.rsi >= 100 - rsi_max)
    for h in list(HORIZONS) + ["eod"]:
        print(line(f"mirror, +{h} bars", tab.loc[mirror, f"f{h}"]))
    print("\n  A SHORT of the mirror earns the negative of these numbers.")
    print("  If longs below VWAP lose and shorts above VWAP also lose, the")
    print("  signal is noise. If one wins, there is a direction worth having.")

    # ---------------------------------------------------------------- 5
    print()
    print("=" * 84)
    print("  5. HOW THE EDGE MOVES WITH THE DEVIATION THRESHOLD")
    print("     (does being MORE stretched help at all? 2-hour horizon)")
    print("=" * 84)
    print(HEAD)
    for lo, hi in [(0.002, 0.004), (0.004, 0.006), (0.006, 0.008),
                   (0.008, 0.012), (0.012, 0.020), (0.020, 1.0)]:
        m = (tab.dev >= lo) & (tab.dev < hi) & (tab.rsi <= rsi_max)
        print(line(f"dev {lo*100:.1f}-{hi*100:.1f}%, RSI<={rsi_max}", tab.loc[m, "f8"]))

    # ---------------------------------------------------------------- 6
    print()
    print("=" * 84)
    print("  6. AND WITH RSI (dev at the live floor, 2-hour horizon)")
    print("=" * 84)
    print(HEAD)
    for lo, hi in [(0, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 50)]:
        m = (tab.dev >= dev_floor) & (tab.rsi >= lo) & (tab.rsi < hi)
        print(line(f"RSI {lo}-{hi}", tab.loc[m, "f8"]))

    # ---------------------------------------------------------------- 7
    print()
    print("=" * 84)
    print("  7. VERDICT")
    print("=" * 84)
    best = None
    for h in list(HORIZONS) + ["eod"]:
        x = pd.Series(tab.loc[setup, f"f{h}"]).dropna()
        if len(x) >= 30 and (best is None or x.mean() > best[1]):
            best = (h, x.mean(), tstat(x), len(x))
    if best is None:
        print("  not enough setups to judge")
        return
    h, mu, t, n = best
    print(f"  Best horizon for the live setup: +{h} bars")
    print(f"  mean forward return {mu*100:+.4f}% over {n:,} occurrences, t={t:+.2f}")
    print(f"  round-trip cost      {cost*100:.4f}%")
    print(f"  net of cost          {(mu-cost)*100:+.4f}%")
    print()
    if mu <= 0:
        print("  >> The premise does not hold. Price below VWAP with low RSI does")
        print("     not revert on this data - it drifts the wrong way or nowhere.")
        print("     No stop, target or filter downstream can fix a negative input.")
    elif mu < cost:
        print("  >> There IS a drift, but it is smaller than the cost of capturing")
        print("     it. This is a real edge that cannot be monetised at retail")
        print("     friction - and note this number is an upper bound, since it")
        print("     ignores the stop that would have ended many of these early.")
    elif abs(t) < 2:
        print("  >> Positive and larger than cost, but not statistically distinct")
        print("     from zero. More data before acting on it.")
    else:
        print("  >> The premise holds and clears cost. The loss is downstream:")
        print("     trigger, stop placement, sizing or the filter stack.")


if __name__ == "__main__":
    main()
