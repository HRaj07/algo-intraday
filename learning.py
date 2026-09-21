"""
Adaptive position sizing that learns from wins AND losses - at a rate the
evidence justifies.

THE PROBLEM WITH NAIVE ADAPTATION
---------------------------------
"Trade bigger after wins, smaller after losses" sounds obviously right and is
how most retail systems destroy themselves. With a 45% win rate, a five-loss
streak happens about 5% of the time at any point - you should expect several per
hundred trades from a perfectly good strategy. A system that reacts to each one
is fitting to noise. And reacting to wins is worse: it sizes up exactly when a
lucky run, not an edge, produced them.

v1 is the case study. Every one of its parameters was a reaction to observed
results (RSI 25->28, deviation 0.8%->0.6%, stop 0.70%->0.55%, shorts off, max
trades 3->5), each justified with a profit-factor number. Net outcome:
-Rs11,317. Automating that loop runs the same mistake faster.

THE FIX: SHRINKAGE
------------------
Don't ask "what is my win rate?" Ask "what should I believe my win rate is,
given a prior and n observations?" The Bayesian answer shrinks the observed rate
toward the prior with weight n / (n + k):

    posterior = (k * prior + n * observed) / (k + n)

With k = 50:
    after  10 trades, a 0% win rate moves your belief from 45% to 37.5%
    after  50 trades, it moves to 22.5%
    after 200 trades, it moves to 9%

So the system DOES learn from every trade - it just refuses to overreact to ten
of them. The learning rate is set by the evidence, not by impatience. This is
the difference between adaptation and superstition.

WHAT THIS MODULE MAY AND MAY NOT DO
-----------------------------------
It may scale POSITION SIZE, within hard bounds.
It may NOT touch entry rules, exit rules, filters or thresholds. Those change
only when a human reviews analyse_trades.py output and decides. Sizing is
recoverable; a loosened filter that lets in unprofitable trades is not.
"""
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# PRIORS - what we believe before the live book has anything to say
# --------------------------------------------------------------------------
# Deliberately modest. The strategy is unvalidated, so the prior is "roughly
# breakeven", not the backtest's optimism. If live results are good, the
# posterior climbs; if bad, it falls. Either way it starts honest.
PRIOR_WIN_RATE = 0.45
PRIOR_AVG_WIN_R = 1.50       # scale-out at VWAP then trail
PRIOR_AVG_LOSS_R = 1.00      # stop is 1R by construction
PRIOR_STRENGTH = 50          # k: how many real trades it takes to move belief

# --------------------------------------------------------------------------
# BOUNDS - the system cannot size itself into trouble or into irrelevance
# --------------------------------------------------------------------------
MIN_RISK_PCT = 0.0020        # 0.20% floor: never so small the trade is pointless
MAX_RISK_PCT = 0.0060        # 0.60% ceiling: never more than 1.5x base
BASE_RISK_PCT = 0.0040       # 0.40%, the config default

PERF_SCALAR_MIN = 0.60
PERF_SCALAR_MAX = 1.20       # asymmetric on purpose: quicker to de-risk than to
                             # add risk. Being wrong about a drawdown costs more
                             # than being wrong about a good run.

# Drawdown de-risking. Independent of the performance scalar: this responds to
# where equity IS, not to what the win rate implies.
DD_FULL_RISK_BELOW = 0.02    # under 2% drawdown, no reduction
DD_HALF_RISK_AT = 0.05       # at 5%, half size (the halt fires at 6%)


def posterior_expectancy(trades: List[Dict]) -> Tuple[float, float, int]:
    """
    Expectancy in R, shrunk toward the prior by sample size.

    Returns (posterior_expectancy_R, prior_expectancy_R, n_trades).
    Closed trades only; partial exits count individually, which is correct -
    each is a real realised outcome.
    """
    prior_exp = (PRIOR_WIN_RATE * PRIOR_AVG_WIN_R
                 - (1 - PRIOR_WIN_RATE) * PRIOR_AVG_LOSS_R)

    rs = [t.get("R_multiple", 0.0) for t in trades if "R_multiple" in t]
    n = len(rs)
    if n == 0:
        return prior_exp, prior_exp, 0

    observed = sum(rs) / n
    k = PRIOR_STRENGTH
    posterior = (k * prior_exp + n * observed) / (k + n)
    return posterior, prior_exp, n


def performance_scalar(trades: List[Dict]) -> Tuple[float, str]:
    """
    How much to scale risk, based on what the evidence supports believing.

    The ratio of posterior to prior expectancy, bounded. Because the posterior
    shrinks toward the prior, a short losing streak barely moves this - which is
    the entire point.
    """
    post, prior, n = posterior_expectancy(trades)

    if n < 10:
        return 1.0, f"n={n}, too few trades to infer anything - base size"

    if prior <= 0:
        return 1.0, "prior expectancy non-positive, scalar disabled"

    raw = post / prior
    scalar = max(PERF_SCALAR_MIN, min(PERF_SCALAR_MAX, raw))

    direction = "trimming" if scalar < 0.99 else ("adding" if scalar > 1.01 else "holding")
    return scalar, (
        f"n={n}, observed expectancy shrunk to {post:+.3f}R vs prior {prior:+.3f}R "
        f"-> {direction} ({scalar:.2f}x)"
    )


def drawdown_scalar(equity: float, peak: float) -> Tuple[float, str]:
    """
    De-risk as equity falls from its high-water mark.

    This is not a prediction that losses continue. It is sequence-risk control:
    smaller positions during a drawdown mean the recovery needs a smaller gain,
    and the account has more room before the 6% halt.
    """
    if peak <= 0 or equity >= peak:
        return 1.0, "at high-water mark"

    dd = (peak - equity) / peak
    if dd <= DD_FULL_RISK_BELOW:
        return 1.0, f"drawdown {dd*100:.1f}% - within normal range"

    span = DD_HALF_RISK_AT - DD_FULL_RISK_BELOW
    frac = min(1.0, (dd - DD_FULL_RISK_BELOW) / span)
    scalar = 1.0 - 0.5 * frac
    return scalar, f"drawdown {dd*100:.1f}% - sizing at {scalar:.2f}x"


def adaptive_risk_pct(trades: List[Dict], equity: float, peak: float,
                      base: float = BASE_RISK_PCT) -> Tuple[float, Dict]:
    """
    The one number this module exists to produce.

    risk% = base x performance_scalar x drawdown_scalar, clamped to [0.20%, 0.60%].

    Both scalars are reported so every sizing decision is auditable after the
    fact. If this ever produces a number you cannot explain, that is a bug.
    """
    perf, perf_why = performance_scalar(trades)
    dd, dd_why = drawdown_scalar(equity, peak)

    raw = base * perf * dd
    final = max(MIN_RISK_PCT, min(MAX_RISK_PCT, raw))

    return final, {
        "base_pct": base,
        "performance_scalar": round(perf, 3),
        "performance_reason": perf_why,
        "drawdown_scalar": round(dd, 3),
        "drawdown_reason": dd_why,
        "raw_pct": round(raw, 5),
        "final_pct": round(final, 5),
        "clamped": abs(raw - final) > 1e-9,
    }


def explain(trades: List[Dict], equity: float, peak: float) -> str:
    """Human-readable sizing decision, for the log every run."""
    pct, d = adaptive_risk_pct(trades, equity, peak)
    lines = [
        f"SIZING: {pct*100:.3f}% of equity (Rs{equity*pct:,.0f} risk per trade)",
        f"  base {d['base_pct']*100:.2f}%",
        f"  performance x{d['performance_scalar']}: {d['performance_reason']}",
        f"  drawdown    x{d['drawdown_scalar']}: {d['drawdown_reason']}",
    ]
    if d["clamped"]:
        lines.append(f"  clamped to the [{MIN_RISK_PCT*100:.2f}%, {MAX_RISK_PCT*100:.2f}%] band")
    return "\n".join(lines)
