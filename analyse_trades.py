"""
Trade journal and review report.

This is the half of "learn from wins and losses" that a human stays inside.
It reads the live trade record, slices it every way that could matter, and for
each slice reports the numbers ALONGSIDE whether the sample can support a
conclusion. Then it says what it would take to actually know.

Why it refuses to be clever: with n=12 a 75% win rate is entirely consistent
with a coin. v1's whole failure was reading patterns of that size as findings
and editing config.py. This tool prints the pattern and the caveat in the same
breath, so the caveat cannot be skipped.

    python analyse_trades.py              # full report
    python analyse_trades.py --json       # machine-readable
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config import SECTOR
from learning import (adaptive_risk_pct, posterior_expectancy, PRIOR_STRENGTH,
                      PRIOR_WIN_RATE)

STATE = Path("logs/paper_state_v2.json")

# Below this, a slice cannot support a decision. Chosen from the power
# calculation: detecting a 45%->55% win-rate shift at 80% power needs ~392
# trades. 30 is where a slice stops being pure anecdote, not where it becomes
# evidence.
MIN_N_TO_DISCUSS = 8
MIN_N_TO_ACT = 30


def wilson(k: int, n: int, z: float = 1.96):
    """Wilson score interval - honest about small n, unlike the naive one."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - s) / d, (c + s) / d)


def binom_p(k: int, n: int, p0: float = 0.5) -> float:
    """Two-sided probability of k or more extreme, under p0. No scipy needed."""
    if n == 0:
        return 1.0
    def pmf(i):
        return math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i)
    obs = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= obs * 1.0000001))


def verdict(n: int, k: int) -> str:
    if n < MIN_N_TO_DISCUSS:
        return "anecdote"
    p = binom_p(k, n)
    if n < MIN_N_TO_ACT:
        return f"too few to act (p={p:.2f})"
    if p < 0.05:
        return f"SIGNIFICANT (p={p:.3f})"
    return f"not significant (p={p:.2f})"


def load():
    if not STATE.exists():
        print(f"No trade record at {STATE} yet. Nothing to review.")
        print("v2 creates it on its first filled position.")
        sys.exit(0)
    return json.load(open(STATE))


def slice_report(title: str, groups: dict, note: str = "") -> list:
    """One breakdown table. Returns rows for the JSON output too."""
    print(f"\n{title}")
    if note:
        print(f"  {note}")
    print(f"  {'bucket':<20} {'n':>4} {'wins':>5} {'win%':>6} {'net P&L':>11} "
          f"{'avg R':>7}  {'verdict':<24}")
    rows = []
    for key in sorted(groups, key=lambda k: sum(t["pnl"] for t in groups[k])):
        ts = groups[key]
        n = len(ts)
        k = sum(1 for t in ts if t["pnl"] > 0)
        net = sum(t["pnl"] for t in ts)
        avg_r = sum(t.get("R_multiple", 0) for t in ts) / n
        v = verdict(n, k)
        print(f"  {str(key):<20} {n:>4} {k:>5} {k/n*100:>5.0f}% {net:>11,.0f} "
              f"{avg_r:>+7.2f}  {v:<24}")
        rows.append({"bucket": str(key), "n": n, "wins": k, "net_pnl": round(net),
                     "avg_R": round(avg_r, 3), "verdict": v})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    st = load()
    h = st.get("trade_history", [])
    if not h:
        print("No closed trades yet.")
        return 0

    n = len(h)
    wins = sum(1 for t in h if t["pnl"] > 0)
    net = sum(t["pnl"] for t in h)
    gp = sum(t["pnl"] for t in h if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in h if t["pnl"] <= 0))
    friction = sum(t.get("friction", 0) for t in h)
    lo, hi = wilson(wins, n)

    print("=" * 78)
    print(f"TRADE REVIEW  |  {datetime.now():%Y-%m-%d %H:%M}  |  {n} closed trades")
    print("=" * 78)
    print(f"  Net P&L          Rs{net:>12,.0f}")
    print(f"  Gross profit     Rs{gp:>12,.0f}")
    print(f"  Gross loss       Rs{gl:>12,.0f}")
    print(f"  Friction paid    Rs{friction:>12,.0f}   "
          f"({friction/(gp+gl)*100 if (gp+gl) else 0:.1f}% of gross turnover in P&L terms)")
    print(f"  Profit factor    {gp/gl if gl else float('inf'):>12.2f}")
    print(f"  Win rate         {wins/n*100:>11.1f}%   "
          f"95% CI [{lo*100:.0f}%, {hi*100:.0f}%]")

    # The single most important line in this report.
    if hi - lo > 0.25:
        print(f"\n  >> That confidence interval is {(hi-lo)*100:.0f} points wide. At n={n} you")
        print( "     cannot distinguish this strategy from most others. Do not act on it.")

    # ---------------- is it beating costs? ----------------
    print("\n" + "-" * 78)
    print("THE QUESTION THAT KILLED v1: is it gross-profitable but net-negative?")
    print("-" * 78)
    gross_net = net + friction
    print(f"  Gross P&L (before friction)  Rs{gross_net:>12,.0f}")
    print(f"  Friction                     Rs{-friction:>12,.0f}")
    print(f"  Net                          Rs{net:>12,.0f}")
    if gross_net > 0 and net < 0:
        print("\n  >> PICKING WINNERS, LOSING TO COSTS. This is exactly v1's failure.")
        print("     Widen the stop (bigger stop = smaller position = less friction),")
        print("     or raise min_reward_risk_after_cost so marginal trades are rejected.")
    elif gross_net <= 0:
        print("\n  >> Negative even before costs. The signal itself is not working;")
        print("     no amount of cost engineering fixes this one.")
    else:
        print("\n  >> Net positive after real costs.")

    out = {"summary": {"n": n, "wins": wins, "net_pnl": round(net),
                       "profit_factor": round(gp / gl, 2) if gl else None,
                       "friction": round(friction),
                       "win_rate_ci": [round(lo, 3), round(hi, 3)]}}

    # ---------------- breakdowns ----------------
    print("\n" + "=" * 78)
    print("BREAKDOWNS  —  read the verdict column before the P&L column")
    print("=" * 78)

    by_reason = defaultdict(list)
    for t in h:
        by_reason[t["reason"]].append(t)
    out["by_exit"] = slice_report(
        "By exit reason", by_reason,
        "Lots of square_off or time_stop means trades are not resolving in time.")

    by_hour = defaultdict(list)
    for t in h:
        try:
            by_hour[t["entry_time"][11:13] + ":00"].append(t)
        except Exception:
            pass
    out["by_hour"] = slice_report(
        "By entry hour (IST)", by_hour,
        "v1 lost Rs13,326 on entries after 13:30. v2 blocks them; this is the check.")

    by_ticker = defaultdict(list)
    for t in h:
        by_ticker[t["ticker"]].append(t)
    out["by_ticker"] = slice_report(
        "By ticker", by_ticker,
        "Per-name samples are tiny for a long time. Expect 'anecdote' here for months.")

    by_sector = defaultdict(list)
    for t in h:
        by_sector[SECTOR.get(t["ticker"], "OTHER")].append(t)
    out["by_sector"] = slice_report("By sector", by_sector)

    by_dow = defaultdict(list)
    for t in h:
        try:
            d = datetime.strptime(t["entry_time"][:10], "%Y-%m-%d")
            by_dow[d.strftime("%a")].append(t)
        except Exception:
            pass
    out["by_weekday"] = slice_report(
        "By weekday", by_dow,
        "Almost always noise. Included so you can see it IS noise.")

    # ---------------- calibration ----------------
    # Does the system's own prediction predict? This is the one breakdown that
    # is not fishing: rr_after_cost is a number the strategy commits to at entry,
    # so checking it is calibration, not a search for patterns. If realised R
    # does not rise with predicted R:R, the cost hurdle is not doing its job.
    print("\n" + "=" * 78)
    print("CALIBRATION  —  does predicted reward:risk predict realised R?")
    print("=" * 78)
    with_pred = [t for t in h if t.get("predicted_rr") is not None]
    if not with_pred:
        print("  No trades carry a predicted_rr yet.")
        print("  Recording started with the entry-snapshot change; trades taken")
        print("  before it cannot be assessed. Come back once the book has filled.")
        out["calibration"] = {"n_with_prediction": 0}
    else:
        tiers = defaultdict(list)
        for t in with_pred:
            r = t["predicted_rr"]
            tier = "1.5-2.0" if r < 2.0 else ("2.0-2.75" if r < 2.75 else "2.75+")
            tiers[tier].append(t)
        print(f"  {len(with_pred)} of {n} trades carry a prediction\n")
        print(f"  {'predicted R:R':<14} {'n':>4} {'realised avg R':>15} "
              f"{'win%':>6} {'net P&L':>11}  {'verdict':<24}")
        rows = []
        for tier in ["1.5-2.0", "2.0-2.75", "2.75+"]:
            ts = tiers.get(tier)
            if not ts:
                continue
            m = len(ts)
            k = sum(1 for t in ts if t["pnl"] > 0)
            avg_r = sum(t.get("R_multiple", 0) for t in ts) / m
            net = sum(t["pnl"] for t in ts)
            print(f"  {tier:<14} {m:>4} {avg_r:>+15.3f} {k/m*100:>5.0f}% "
                  f"{net:>11,.0f}  {verdict(m, k):<24}")
            rows.append({"tier": tier, "n": m, "avg_R": round(avg_r, 3),
                         "net_pnl": round(net)})
        out["calibration"] = {"n_with_prediction": len(with_pred), "tiers": rows}
        if len(rows) >= 2:
            ordered = all(rows[i]["avg_R"] <= rows[i+1]["avg_R"]
                          for i in range(len(rows) - 1))
            thin = any(r["n"] < MIN_N_TO_ACT for r in rows)
            print()
            if thin:
                print("  >> At least one tier is under 30 trades. Monotonicity here is")
                print("     not yet evidence either way - the ordering will flip on noise.")
            elif ordered:
                print("  >> Realised R rises with predicted R:R. The cost hurdle is")
                print("     ranking trades correctly; raising it would concentrate quality.")
            else:
                print("  >> Realised R does NOT rise with predicted R:R. The hurdle is")
                print("     filtering, but not RANKING. Do not allocate more to high tiers.")

    # ---------------- what sizing is doing ----------------
    print("\n" + "=" * 78)
    print("ADAPTIVE SIZING  —  what the record currently supports believing")
    print("=" * 78)
    eq = st.get("cash", 0) + sum(p.get("margin", 0)
                                 for p in st.get("positions", {}).values())
    peak = st.get("equity_peak", eq)
    post, prior, nn = posterior_expectancy(h)
    pct, detail = adaptive_risk_pct(h, eq, peak)
    obs = sum(t.get("R_multiple", 0) for t in h) / n if n else 0
    print(f"  Observed expectancy   {obs:+.3f}R over {nn} trades")
    print(f"  Prior expectancy      {prior:+.3f}R")
    print(f"  Posterior (shrunk)    {post:+.3f}R    "
          f"weight on observation: {nn/(nn+PRIOR_STRENGTH)*100:.0f}%")
    print(f"  -> risk per trade     {pct*100:.3f}% of equity")
    print(f"     performance x{detail['performance_scalar']}  "
          f"drawdown x{detail['drawdown_scalar']}")
    print(f"\n  The posterior moves toward what you observe as n grows. At n={nn} your")
    print(f"  observations carry {nn/(nn+PRIOR_STRENGTH)*100:.0f}% of the weight; "
          f"at n=200 they carry {200/(200+PRIOR_STRENGTH)*100:.0f}%.")
    out["sizing"] = detail

    # ---------------- what would it take to know ----------------
    print("\n" + "=" * 78)
    print("WHAT WOULD IT TAKE TO ACTUALLY KNOW?")
    print("=" * 78)
    for p1, p2, lab in [(0.45, 0.55, "a large effect (45% -> 55%)"),
                        (0.45, 0.50, "a modest effect (45% -> 50%)")]:
        za, zb = 1.96, 0.84
        pbar = (p1 + p2) / 2
        need = math.ceil(2 * (za + zb) ** 2 * pbar * (1 - pbar) / (p2 - p1) ** 2)
        print(f"  {lab:<32} {need:>6,} trades   (you have {n})")
    print("\n  Until then, changes to entry/exit rules are guesses wearing a number.")
    print("  Run backtest_honest.py on out-of-sample data before changing anything.")

    if a.json:
        print("\n" + json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
