"""
Is the momentum reading real, or an artefact of measuring from a close you
cannot trade at?

    python3 study_momentum.py

study_signal_edge.py found that the mirror of our setup - price stretched ABOVE
VWAP with high RSI - returns +0.127% to the session close (t=+6.35), while the
setup we actually trade returns -0.137%. That is the only positive number in
the study, so it deserves the hardest possible look before anyone builds on it.

Three ways a result like that turns out to be nothing:

  1. LOOKAHEAD. study_signal_edge.py measures from the signal bar's close. By
     the time a 15-minute bar has closed and the scan has run, that price is
     gone. Everything here enters at the NEXT bar's open, which is the earliest
     price a real order could get.

  2. ONE GOOD MONTH. 59 trading days is one regime. Split it and see whether
     both halves agree.

  3. A FEW NAMES CARRYING IT. A mean of +0.127% across 5,132 observations can
     be four names and a takeover rumour. Per-name dispersion says which.

There is a fourth that this file cannot settle and no amount of care will:
the sample is 59 days long because yfinance serves 60 days of 15-minute bars.
There is no out-of-sample period available. Anything found here is a
hypothesis, not a validated edge.
"""
import pickle
from datetime import time as dtime

import numpy as np
import pandas as pd

from config import STRATEGY, SYSTEM, INDEX_TICKER, VIX_TICKER
from costs import VARIABLE_ROUNDTRIP_PCT
from data.fetcher import TechnicalIndicators

CACHE = "cache_recent_15m.pkl"
TI = TechnicalIndicators()
COST = VARIABLE_ROUNDTRIP_PCT


def tstat(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    return 0.0 if sd == 0 else x.mean() / (sd / np.sqrt(len(x)))


def build(data):
    first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
    last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))
    rows = []
    for tkr, df in data.items():
        if tkr in (INDEX_TICKER, VIX_TICKER) or len(df) < 100:
            continue
        df = df.sort_index()
        rsi = TI.rsi(df, STRATEGY["rsi_period"])
        atr = TI.atr(df, STRATEGY["atr_period"])
        vwap = TI.vwap(df)
        close, opn, high, low = df["close"], df["open"], df["high"], df["low"]
        dates = pd.Series(df.index.date, index=df.index)

        # THE ENTRY: next bar's open. Same day only.
        entry = opn.shift(-1)
        entry[dates.shift(-1) != dates] = np.nan

        eod_close = close.groupby(dates.values).transform("last")
        # exits, all measured FROM the tradeable entry
        r_eod = eod_close / entry - 1.0
        r_h = {}
        for h in (2, 4, 8):
            px = close.shift(-(h + 1))
            px[dates.shift(-(h + 1)) != dates] = np.nan
            r_h[h] = px / entry - 1.0

        # worst drawdown between entry and close, for stop feasibility
        rev_low = low[::-1].groupby(dates.values[::-1]).cummin()[::-1]
        mae = rev_low.shift(-1) / entry - 1.0
        rev_high = high[::-1].groupby(dates.values[::-1]).cummax()[::-1]
        mfe = rev_high.shift(-1) / entry - 1.0

        dev = (vwap - close) / close          # positive = BELOW vwap
        t = pd.Series(df.index.time, index=df.index)
        keep = ((t >= first) & (t <= last) & dev.notna() & rsi.notna()
                & atr.notna() & entry.notna() & (close > 0))

        rows.append(pd.DataFrame({
            "ticker": tkr, "date": dates, "dev": dev, "rsi": rsi,
            "atr_pct": atr / close, "eod": r_eod, "mae": mae, "mfe": mfe,
            "h2": r_h[2], "h4": r_h[4], "h8": r_h[8],
        })[keep])
    return pd.concat(rows, ignore_index=True)


def block(title, x, extra=""):
    x = pd.Series(x).dropna().values
    if len(x) < 30:
        print(f"  {title:<30}{len(x):>7,}   (too few)")
        return
    print(f"  {title:<30}{len(x):>7,}  {x.mean()*100:>+8.3f}%  "
          f"{(x.mean()-COST)*100:>+8.3f}%  {(x>0).mean()*100:>6.1f}%  {tstat(x):>+7.2f}  {extra}")


HEAD = f"  {'':<30}{'n':>7}  {'gross':>9}  {'net':>9}  {'win%':>6}  {'t':>7}"


def main():
    data = pickle.load(open(CACHE, "rb"))
    tab = build(data)
    print(f"{len(tab):,} bars, {tab.ticker.nunique()} names, "
          f"{tab.date.nunique()} days")
    print(f"entry = NEXT bar's open (tradeable). cost = {COST*100:.4f}% round trip\n")

    dev_floor = STRATEGY["min_vwap_deviation"]
    mom = (tab.dev <= -dev_floor) & (tab.rsi >= 60)     # stretched ABOVE vwap
    rev = (tab.dev >= dev_floor) & (tab.rsi <= STRATEGY["rsi_oversold"])

    print("=" * 86)
    print("  1. THE TWO CONDITIONS, ENTERED AT A PRICE YOU COULD ACTUALLY GET")
    print("=" * 86)
    print(HEAD)
    print("  -- momentum: long, stretched ABOVE vwap, RSI>=60 --")
    for k, lbl in [("h2", "+30 min"), ("h4", "+1 hour"), ("h8", "+2 hours"), ("eod", "to close")]:
        block(f"momentum {lbl}", tab.loc[mom, k])
    print("  -- reversion: long, stretched BELOW vwap, RSI<=40 (what we trade) --")
    for k, lbl in [("h2", "+30 min"), ("h4", "+1 hour"), ("h8", "+2 hours"), ("eod", "to close")]:
        block(f"reversion {lbl}", tab.loc[rev, k])
    print("  -- baseline: every bar --")
    for k, lbl in [("h8", "+2 hours"), ("eod", "to close")]:
        block(f"all bars {lbl}", tab[k])

    print()
    print("=" * 86)
    print("  2. DOES IT HOLD IN BOTH HALVES OF THE SAMPLE?")
    print("=" * 86)
    days = sorted(tab.date.unique())
    mid = days[len(days) // 2]
    print(HEAD)
    for name, m in [("momentum", mom), ("reversion", rev)]:
        for half, sel in [("first half", tab.date < mid), ("second half", tab.date >= mid)]:
            block(f"{name} {half}", tab.loc[m & sel, "eod"])
    print(f"\n  split at {mid}")

    print()
    print("=" * 86)
    print("  3. MONTH BY MONTH (momentum, to close)")
    print("=" * 86)
    print(HEAD)
    months = pd.to_datetime(pd.Series(tab.date)).dt.to_period("M")
    for mo in sorted(months.unique()):
        block(str(mo), tab.loc[mom & (months == mo).values, "eod"])

    print()
    print("=" * 86)
    print("  4. IS IT A FEW NAMES? (momentum, to close, names with n>=20)")
    print("=" * 86)
    g = tab[mom].groupby("ticker")["eod"]
    per = pd.DataFrame({"n": g.size(), "mean": g.mean()})
    per = per[per.n >= 20]
    pos = (per["mean"] > 0).sum()
    print(f"  {len(per)} names with >=20 signals")
    print(f"  {pos} of them positive ({pos/len(per)*100:.0f}%)")
    print(f"  median name mean  {per['mean'].median()*100:+.3f}%")
    print(f"  mean of the means {per['mean'].mean()*100:+.3f}%")
    print("  best 5:  " + ", ".join(f"{t} {v*100:+.2f}%"
          for t, v in per.sort_values('mean', ascending=False)['mean'].head(5).items()))
    print("  worst 5: " + ", ".join(f"{t} {v*100:+.2f}%"
          for t, v in per.sort_values('mean')['mean'].head(5).items()))
    top = per.sort_values("mean", ascending=False).head(5).index
    rest = tab[mom & ~tab.ticker.isin(top)]
    print()
    print(HEAD)
    block("excluding the best 5 names", rest["eod"])

    print()
    print("=" * 86)
    print("  5. CAN A STOP SURVIVE IT? (momentum, to close)")
    print("=" * 86)
    sub = tab[mom]
    print(f"  median adverse excursion before the close  {sub.mae.median()*100:+.3f}%")
    print(f"  median favourable excursion               {sub.mfe.median()*100:+.3f}%")
    print(f"  median ATR                                 {sub.atr_pct.median()*100:.3f}%")
    print()
    print(HEAD)
    for s in (0.004, 0.006, 0.008, 0.012):
        # stopped out if the low ever breaches; otherwise hold to close
        r = np.where(sub.mae <= -s, -s, sub.eod)
        block(f"with a {s*100:.1f}% stop", r)

    print()
    print("=" * 86)
    print("  6. WHAT THIS DOES AND DOES NOT ESTABLISH")
    print("=" * 86)
    print("  59 days is the entire window yfinance serves at 15-minute")
    print("  resolution, so there is no out-of-sample period to hold back. A")
    print("  result that survives every check above is a hypothesis worth")
    print("  testing forward in paper, not an edge that has been validated.")
    print("  Entry at the next bar's open still assumes a fill at that open.")


if __name__ == "__main__":
    main()
