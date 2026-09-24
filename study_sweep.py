"""
A parameter sweep that can only RULE OUT.

    python3 study_sweep.py

HOW TO READ THIS FILE, BEFORE THE NUMBERS
-----------------------------------------
This sweeps the candidate momentum rule over thresholds and exits, running each
cell as a real book (3 concurrent, 5 entries/day, sector caps, real costs,
slippage) on the same 59 days every other study used.

That makes it a search over one period, which is how backtests lie. So it is
being used asymmetrically, and deliberately:

    if the BEST cell is still mediocre  -> decisive. No setting of these
                                           knobs makes this work, and the
                                           search was generous to itself.

    if some cell looks excellent        -> proves nothing. It was chosen
                                           after seeing its own score, on
                                           the same data, with no holdout.

Earlier in this analysis that exact mistake produced a "+0.353% net, t=+5.72"
filter stack that was -0.236% in September. The defence here is the same one
that caught it: every cell reports its three months separately and its
top-5-trade concentration, and a cell that needs one month or five trades is
reported as failing however good its total looks.

WHAT WOULD ACTUALLY SETTLE IT
-----------------------------
Not this. yfinance serves 60 days of 15-minute bars, so no holdout exists at
this resolution. The settling test is either forward paper trading, or the same
signal at daily resolution where years of out-of-sample data do exist.

NOTE ON SPEED
-------------
Indicators and the per-bar candidate table are computed ONCE, up front, and
every cell then filters that table. Recomputing per cell took ~5s a cell and
the sweep could not finish inside a shell timeout; this takes about as long in
total as two cells used to.
"""
import itertools
import pickle
import sys
from datetime import time as dtime

import numpy as np
import pandas as pd

from config import STRATEGY, SYSTEM, RISK, SECTOR, INDEX_TICKER, VIX_TICKER
from costs import cost_in_rupees
from data.fetcher import TechnicalIndicators

CACHE = "cache_recent_15m.pkl"
TI = TechnicalIndicators()


def build_tables(data):
    """
    One row per (name, bar): the features a rule can test, the price an order
    would fill at, and the path the trade would take from there.

    `mae_to` / `low_path` are what let a cell apply any stop without replaying
    bars: for each entry bar we keep the running low and the closing price of
    the session, so a stop is one comparison rather than a loop.
    """
    first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
    last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))
    rows = []
    for tkr, df in data.items():
        if tkr in (INDEX_TICKER, VIX_TICKER) or len(df) < 100:
            continue
        df = df.sort_index()
        rsi = TI.rsi(df, STRATEGY["rsi_period"])
        vwap = TI.vwap(df)
        close, opn, low = df["close"], df["open"], df["low"]
        d = pd.Series(df.index.date, index=df.index)
        t = pd.Series(df.index.time, index=df.index)

        entry = opn.shift(-1)
        entry[d.shift(-1) != d] = np.nan
        eod = close.groupby(d.values).transform("last")
        # lowest low from the bar AFTER entry through the close of the day
        run_low = low[::-1].groupby(d.values[::-1]).cummin()[::-1].shift(-1)

        rvol = df["volume"] / df["volume"].rolling(50, min_periods=20).median()
        turnover = (close * df["volume"]).rolling(50, min_periods=20).median()
        dev = (close - vwap) / vwap

        keep = ((t >= first) & (t <= last) & entry.notna() & vwap.notna()
                & rsi.notna() & rvol.notna()
                & (turnover >= RISK["min_median_15m_turnover"]))
        rows.append(pd.DataFrame({
            "ticker": tkr, "sector": SECTOR.get(tkr, "OTHER"),
            "date": d, "bar": d.groupby(d.values).cumcount(),
            "dev": dev, "rsi": rsi, "rvol": rvol,
            "entry": entry, "eod": eod, "low": run_low,
        })[keep])
    tab = pd.concat(rows, ignore_index=True)
    return tab.sort_values(["date", "bar", "rvol"],
                           ascending=[True, True, False]).reset_index(drop=True)


def run_cell(tab, dev_min, rsi_min, rvol_min, stop_pct, trail, capital):
    """
    First-come-first-served within the portfolio caps, highest rvol first
    within a bar. Positions are held to the close unless the stop is hit.
    """
    sel = tab[(tab.dev >= dev_min) & (tab.rsi >= rsi_min) & (tab.rvol >= rvol_min)]
    if sel.empty:
        return pd.DataFrame()

    equity = capital
    out = []
    maxpos = RISK["max_concurrent_positions"]
    maxday = RISK["max_entries_per_day"]
    maxsec = RISK["max_positions_per_sector"]

    for day, g in sel.groupby("date", sort=True):
        # a position opened at bar b occupies a slot until the close, so
        # concurrency within a day is just "how many are already open"
        held = []          # (ticker, sector)
        entries = 0
        day_rows = []
        for r in g.itertuples():
            if entries >= maxday or len(held) >= maxpos:
                break
            if any(h[0] == r.ticker for h in held):
                continue
            if sum(1 for h in held if h[1] == r.sector) >= maxsec:
                continue
            if not np.isfinite(r.entry) or r.entry <= 0 or not np.isfinite(r.low):
                continue
            risk_ps = r.entry * stop_pct
            qty = int((equity * RISK["risk_pct_per_trade"]) / risk_ps)
            notional = qty * r.entry
            cap_n = equity * RISK["max_notional_per_trade_pct"]
            if notional > cap_n:
                qty = int(cap_n / r.entry)
                notional = qty * r.entry
            if qty < 1 or notional < RISK["min_notional_per_trade"]:
                continue
            stop = r.entry - risk_ps
            if trail and r.eod >= r.entry + risk_ps:
                # if it reached 1R the stop ratcheted to just above breakeven;
                # this is the optimistic reading - it assumes the 1R came first
                stop = max(stop, r.entry * 1.0008)
            if r.low <= stop:
                px, why = stop, "stop"
            else:
                px, why = r.eod, "square_off"
            gross_out = px * qty
            fric = (cost_in_rupees(notional) + cost_in_rupees(gross_out)) / 2
            slip = gross_out * (0.0005 if why == "stop" else 0.0003)
            pnl = (px - r.entry) * qty - fric - slip
            day_rows.append({"date": day, "pnl": pnl, "reason": why,
                             "friction": fric + slip})
            held.append((r.ticker, r.sector))
            entries += 1
        for row in day_rows:
            equity += row["pnl"]
        out.extend(day_rows)
    return pd.DataFrame(out)


def main():
    data = pickle.load(open(CACHE, "rb"))
    print("building the candidate table once...", flush=True)
    tab = build_tables(data)
    cap = SYSTEM["initial_capital"]
    print(f"{len(tab):,} liquid in-window bars, {tab.date.nunique()} days\n")

    grid = list(itertools.product(
        (0.008, 0.012, 0.018),
        (70, 75, 80),
        (2.0, 4.0, 6.0),
        (0.012, 0.016),
        (False, True),
    ))
    print(f"{len(grid)} cells, each a full 59-day book on Rs{cap:,.0f}\n")
    print(f"  {'dev':>5} {'rsi':>4} {'rvol':>5} {'stop':>5} {'trail':>6} "
          f"{'n':>5} {'net Rs':>10} {'%':>7} {'PF':>6} {'t':>6} "
          f"{'Jul':>8} {'Aug':>8} {'Sep':>8} {'ex-top5':>9}")
    rows = []
    for dev, rsi, rvol, stop, trail in grid:
        tr = run_cell(tab, dev, rsi, rvol, stop, trail, cap)
        if len(tr) < 30:
            continue
        p = tr.pnl.values
        sd = p.std(ddof=1)
        t = p.mean() / (sd / np.sqrt(len(p))) if sd else 0.0
        gl = abs(p[p <= 0].sum())
        pf = p[p > 0].sum() / gl if gl else 99.0
        mo = pd.to_datetime(tr.date).dt.strftime("%m")
        per = [tr.pnl[mo == m].sum() for m in ("07", "08", "09")]
        ex5 = np.sort(p)[::-1][5:].sum()
        ok = all(x > 0 for x in per) and ex5 > 0
        rows.append(dict(dev=dev, rsi=rsi, rvol=rvol, stop=stop, trail=trail,
                         n=len(p), net=p.sum(), t=t, pf=pf, ex5=ex5, ok=ok))
        print(f"  {dev*100:>4.1f}% {rsi:>4} {rvol:>5.1f} {stop*100:>4.1f}% "
              f"{str(trail):>6} {len(p):>5} {p.sum():>10,.0f} {p.sum()/cap*100:>6.2f}% "
              f"{pf:>6.2f} {t:>+6.2f} "
              + " ".join(f"{x:>8,.0f}" for x in per)
              + f" {ex5:>9,.0f}" + ("   <-- survives" if ok else ""), flush=True)

    print()
    print("=" * 104)
    print("  WHAT SURVIVED")
    print("=" * 104)
    good = [r for r in rows if r["ok"]]
    print(f"  {len(good)} of {len(rows)} cells were positive in all three months")
    print("  AND still positive with their best 5 trades removed.")
    if good:
        best = max(good, key=lambda r: r["t"])
        print(f"\n  strongest survivor: dev>={best['dev']*100:.1f}% rsi>={best['rsi']} "
              f"rvol>={best['rvol']} stop {best['stop']*100:.1f}% trail={best['trail']}")
        print(f"    Rs{best['net']:,.0f} over {best['n']} trades, "
              f"t={best['t']:+.2f}, PF {best['pf']:.2f}, "
              f"ex-top-5 Rs{best['ex5']:,.0f}")
    ts = [r["t"] for r in rows]
    print(f"\n  across ALL {len(rows)} cells: best t={max(ts):+.2f}, "
          f"median t={np.median(ts):+.2f}, {sum(1 for x in ts if x > 2)} cells above t=2")
    pos = sum(1 for r in rows if r["net"] > 0)
    print(f"  {pos} of {len(rows)} cells made money at all "
          f"({pos/len(rows)*100:.0f}%)")

    print()
    print("=" * 104)
    print("  HOW TO READ WHAT SURVIVED")
    print("=" * 104)
    print("  If the best cell here is around t=2, that is not a strategy that")
    print("  passes - it is the best of ~100 tries on one period, and the best")
    print("  of 100 random tries clears t=2 routinely. The number that would")
    print("  mean something is a cell that is unremarkable here and holds up on")
    print("  data this search has never seen.")


if __name__ == "__main__":
    main()
