"""
Weekly walk-forward: the only form of "self-improvement" that cannot cheat.

    python3 walkforward.py            # fit, score on held-out weeks, decide
    python3 walkforward.py --dry-run  # decide but write nothing

WHY THIS IS NOT AN OPTIMISER
----------------------------
An optimiser refits parameters on recent data and adopts whatever scored best.
That is a machine for learning noise, and this project has already been caught
by it twice in one afternoon: a filter stack that scored +0.353% net at t=+5.72
was -0.236% in the month it had not been shown, and a 100-cell threshold sweep
produced a best cell at t=+1.63 that survived none of its own robustness
checks. Both looked like progress. Neither was.

The difference here is that the parameters for the coming period are chosen
using ONLY data from before the holdout, then scored on the holdout, and the
score is written down BEFORE the period trades. Three consequences:

  1. A choice that only works on the data that chose it is visible immediately,
     because the holdout number sits next to the fit number in the same record.
  2. Nothing is adopted unless it cleared cost on data it never saw.
  3. After a few months, walkforward.jsonl answers the question almost no
     self-tuning bot can answer: DID THE RETUNING HELP? Every record carries
     what the frozen original parameters would have scored on the same holdout.
     If refitting never beats freezing, the honest move is to stop refitting,
     and this file is what would show that.

WHAT IT IS ALLOWED TO CHANGE
----------------------------
Four thresholds, each inside a hard-coded band: how far above VWAP, the RSI
floor, the relative-volume floor, the stop width. That is all.

It cannot change direction, cannot switch strategies, cannot touch risk per
trade, position caps, the universe, the cost model, or any exit rule. The
breakeven trail and time stop stay off permanently - they lost money in every
one of the 50 sweep cells they appeared in, and that is not a parameter to be
rediscovered weekly.

Nothing here touches the 15-minute scan. main.py reads params_live.json if it
exists and falls back to config.MOMENTUM if it does not, so a failed or skipped
walk-forward run leaves the bot running exactly what it ran yesterday.
"""
import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from config import INTRADAY_UNIVERSE, INDEX_TICKER, VIX_TICKER, SYSTEM, MOMENTUM
from costs import VARIABLE_ROUNDTRIP_PCT
from study_sweep import build_tables, run_cell     # ONE implementation, shared
from tzutil import now_ist

OUT = Path("research")
LEDGER = OUT / "walkforward.jsonl"
LIVE = Path("params_live.json")

# The bands. A weekly job may move within these and nowhere else. They are set
# where the four measured gradients turned positive and stayed positive across
# July, August and September independently - not at the sweep's best cell,
# which is the most overfit number available.
BANDS = {
    "min_dev_above_vwap": (0.008, 0.020),
    "min_rsi": (70, 80),
    "min_rvol": (2.0, 6.0),
    "stop_pct_floor": (0.010, 0.020),
}

GRID = list(itertools.product(
    (0.008, 0.012, 0.018),
    (70, 75, 80),
    (2.0, 4.0, 6.0),
    (0.012, 0.016),
))

HOLDOUT_DAYS = 10          # ~2 trading weeks kept back from the fit
MIN_FIT_TRADES = 60        # below this the fit is an anecdote, not a fit
MIN_HOLDOUT_TRADES = 15    # below this the holdout cannot reject anything


def score(trades: pd.DataFrame, capital: float) -> dict:
    if trades is None or len(trades) == 0:
        return {"n": 0}
    p = trades.pnl.values
    sd = p.std(ddof=1) if len(p) > 1 else 0.0
    gl = abs(p[p <= 0].sum())
    months = pd.to_datetime(trades.date).dt.strftime("%Y-%m")
    per = {m: float(trades.pnl[months == m].sum()) for m in sorted(months.unique())}
    return {
        "n": int(len(p)),
        "net": float(p.sum()),
        "pct": float(p.sum() / capital * 100),
        "t": float(p.mean() / (sd / np.sqrt(len(p)))) if sd else 0.0,
        "pf": float(p[p > 0].sum() / gl) if gl else 99.0,
        # The single most useful number in this file. Removing the best five
        # trades turned EVERY cell of the original sweep negative, which is
        # what "the edge is the right tail" means in practice. A parameter set
        # that still stands up without its tail is a different animal.
        "ex_top5": float(np.sort(p)[::-1][5:].sum()) if len(p) > 5 else 0.0,
        "by_month": per,
    }


def robust(s: dict) -> bool:
    """Passes only if it does not depend on one month or five lucky trades."""
    return (s.get("n", 0) >= MIN_FIT_TRADES
            and s.get("net", 0) > 0
            and s.get("ex_top5", 0) > 0
            and len(s.get("by_month", {})) >= 2
            and all(v > 0 for v in s["by_month"].values()))


def fetch():
    from data.fetcher import IntradayFetcher
    syms = INTRADAY_UNIVERSE + [INDEX_TICKER, VIX_TICKER]
    print(f"fetching 60d of 15-minute bars for {len(syms)} symbols...", flush=True)
    return IntradayFetcher().fetch_intraday(syms, days_back=60)


def current_params() -> dict:
    """What the bot is running right now."""
    if LIVE.exists():
        try:
            return json.load(open(LIVE))["params"]
        except Exception:
            pass
    return {k: MOMENTUM[k] for k in BANDS}


def in_bands(p: dict) -> bool:
    return all(BANDS[k][0] <= p[k] <= BANDS[k][1] for k in BANDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    cap = SYSTEM["initial_capital"]
    tab = build_tables(fetch())
    days = sorted(tab.date.unique())
    if len(days) < HOLDOUT_DAYS + 20:
        print(f"only {len(days)} trading days - not enough to split. Nothing changed.")
        return

    cut = days[-HOLDOUT_DAYS]
    fit, hold = tab[tab.date < cut], tab[tab.date >= cut]
    print(f"\nFIT     {days[0]} .. {days[-HOLDOUT_DAYS - 1]}  ({len(days) - HOLDOUT_DAYS} days)")
    print(f"HOLDOUT {cut} .. {days[-1]}  ({HOLDOUT_DAYS} days)  <- never seen by the fit\n")

    # ---------------------------------------------------------------- fit
    print(f"{'dev':>5} {'rsi':>4} {'rvol':>5} {'stop':>5}  {'n':>4} "
          f"{'net':>10} {'ex-top5':>10} {'t':>6}  robust")
    candidates = []
    for dev, rsi, rvol, stop in GRID:
        s = score(run_cell(fit, dev, rsi, rvol, stop, False, cap), cap)
        if not s.get("n"):
            continue
        ok = robust(s)
        print(f"{dev*100:>4.1f}% {rsi:>4} {rvol:>5.1f} {stop*100:>4.1f}%  {s['n']:>4} "
              f"{s['net']:>10,.0f} {s['ex_top5']:>10,.0f} {s['t']:>+6.2f}  "
              f"{'yes' if ok else ''}")
        if ok:
            candidates.append(((dev, rsi, rvol, stop), s))

    live = current_params()
    if not candidates:
        print("\nNo parameter set survived the robustness checks on the fit window.")
        print("Keeping what is already running. This is the expected outcome most")
        print("weeks and is not a failure - it is the filter working.")
        record(live, None, None, "no robust candidate", a.dry_run)
        return

    # Rank by the tail-independent number, NOT by raw net. The best raw cell is
    # usually the one whose top five trades were luckiest.
    best_params, best_fit = max(candidates, key=lambda c: c[1]["ex_top5"])
    proposal = dict(zip(BANDS, best_params))
    print(f"\nbest robust candidate on the fit window: {proposal}")
    print(f"  fit: Rs{best_fit['net']:,.0f} over {best_fit['n']} trades, "
          f"t={best_fit['t']:+.2f}, ex-top5 Rs{best_fit['ex_top5']:,.0f}")

    # ------------------------------------------------------------ holdout
    hs = score(run_cell(hold, *best_params, False, cap), cap)
    ls = score(run_cell(hold, *[live[k] for k in BANDS], False, cap), cap)
    print(f"\nHOLDOUT - the number that decides")
    print(f"  proposal : {hs.get('n', 0):>3} trades  Rs{hs.get('net', 0):>10,.0f}  "
          f"t={hs.get('t', 0):+.2f}")
    print(f"  current  : {ls.get('n', 0):>3} trades  Rs{ls.get('net', 0):>10,.0f}  "
          f"t={ls.get('t', 0):+.2f}")

    # ------------------------------------------------------------- decide
    if hs.get("n", 0) < MIN_HOLDOUT_TRADES:
        why = f"holdout produced only {hs.get('n', 0)} trades - cannot reject anything"
        adopt = False
    elif hs.get("net", 0) <= 0:
        why = "proposal lost money on data it had not seen"
        adopt = False
    elif hs.get("net", 0) <= ls.get("net", 0):
        why = "proposal did not beat the parameters already running"
        adopt = False
    elif not in_bands(proposal):
        why = "proposal outside the permitted bands"
        adopt = False
    else:
        why = "cleared cost on unseen data and beat the incumbent"
        adopt = True

    print(f"\nDECISION: {'ADOPT' if adopt else 'KEEP CURRENT'} - {why}")
    if not adopt:
        print("  Keeping current parameters. Most weeks should end here.")
    record(proposal if adopt else live, best_fit, hs, why, a.dry_run,
           incumbent=ls, adopted=adopt, proposal=proposal)


def record(params, fit_s, hold_s, why, dry, incumbent=None, adopted=False,
           proposal=None):
    """
    Append the decision BEFORE the period it applies to trades.

    This ordering is the whole point. A record written afterwards can be
    rationalised; one written in advance is a prediction, and a file of
    predictions is the only way to find out whether the refitting is worth
    doing at all.
    """
    rec = {
        "decided_at": str(now_ist()),
        "adopted": adopted,
        "why": why,
        "params_now_live": params,
        "proposal": proposal,
        "fit": fit_s,
        "holdout_proposal": hold_s,
        "holdout_incumbent": incumbent,
        "cost_model_roundtrip_pct": VARIABLE_ROUNDTRIP_PCT,
    }
    if dry:
        print("\n--dry-run: nothing written\n")
        print(json.dumps(rec, indent=2, default=str))
        return
    OUT.mkdir(exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    json.dump({"params": params, "decided_at": rec["decided_at"], "why": why},
              open(LIVE, "w"), indent=2)
    print(f"\nwrote {LEDGER} and {LIVE}")
    summarise()


def summarise():
    """Has the retuning actually helped? Read this before trusting any of it."""
    if not LEDGER.exists():
        return
    recs = [json.loads(l) for l in open(LEDGER) if l.strip()]
    scored = [r for r in recs
              if r.get("holdout_proposal", {}).get("n", 0) >= MIN_HOLDOUT_TRADES
              and r.get("holdout_incumbent")]
    if len(scored) < 4:
        print(f"\n{len(recs)} decision(s) recorded. After about 8 weeks this will")
        print("say whether refitting beats leaving the parameters alone.")
        return
    prop = sum(r["holdout_proposal"].get("net", 0) for r in scored)
    inc = sum(r["holdout_incumbent"].get("net", 0) for r in scored)
    print(f"\nover {len(scored)} scored weeks, on held-out data:")
    print(f"  refitted parameters  Rs{prop:,.0f}")
    print(f"  leaving them alone   Rs{inc:,.0f}")
    if prop <= inc:
        print("  >> Refitting is NOT earning its keep. The honest move is to freeze")
        print("     the parameters and stop running this job.")


if __name__ == "__main__":
    sys.exit(main())
