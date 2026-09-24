"""
Intraday Algo - Configuration v2

Every number below is justified by a stated mechanism, not by a backtest sweep.
Numbers that came only from a sweep are marked UNVALIDATED and must survive the
walk-forward in backtest_honest.py before they are trusted.

Design principle: cost per unit of risk is the first constraint, and the edge
has to clear it. v1 inverted this - it optimised a PF number against a cost
model that was 7x too low, then deployed the result.
"""
from costs import VARIABLE_ROUNDTRIP_PCT

# ---------------------------------------------------------------------------
# UNIVERSE - now lives in universe.py (214 names). See that file for why it
# grew from 25: the 1.22% deviation floor and my hand-cut universe were fighting
# each other, and the measured result was ~4 trades a month.
# ---------------------------------------------------------------------------
from universe import INTRADAY_UNIVERSE, SECTOR, sector_of  # noqa: F401

INDEX_TICKER = "^NSEI"      # NIFTY 50 - drives the regime gate
VIX_TICKER = "^INDIAVIX"

# ---------------------------------------------------------------------------
# SYSTEM
# ---------------------------------------------------------------------------
SYSTEM = {
    "mode": "paper",
    "initial_capital": 1_000_000,   # was 500_000

    # Why not higher: capital and universe size pull against each other. Every
    # rupee demands proportionally more liquidity, and the turnover floor that
    # keeps the 5bps slippage assumption honest rises with it. At Rs50L you need
    # names doing Rs625cr/day - maybe 30 on the NSE - which collapses a 214-name
    # universe and undoes the reason for expanding it. Rs10L needs Rs125cr/day,
    # which most of the list clears.
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "market_open": "09:15",
    "market_close": "15:30",

    # 15:05, not 15:15. Zerodha's own MIS auto-square-off runs at 15:12 (CAS)
    # / 15:25 (non-CAS) and fills at market with no regard for your price.
    # Never let the broker close your position for you.
    "square_off_time": "15:05",

    # No new entries after 13:15. A mean-reversion trade needs time to revert;
    # v1's median hold was 105 minutes, and 55% of all its exits were the clock
    # rather than the thesis. Entries after 13:30 in v1 lost Rs13,326 across 22
    # trades while earlier entries made Rs2,009 across 27.
    # HONESTY NOTE: that split has permutation p=0.078 on n=49 and the cutoff
    # was chosen after seeing the data, so it is NOT independently validated.
    # It is adopted because the mechanism is sound - you cannot let a trade
    # work if you have already run out of session.
    "last_entry_time": "13:15",
    "first_entry_time": "09:45",     # skip the opening auction noise
}

# ---------------------------------------------------------------------------
# RISK  - the part that actually decides whether you survive
# ---------------------------------------------------------------------------
RISK = {
    # Risk per trade as a FRACTION of current equity, not a fixed rupee amount.
    # Fixed rupee risk means you keep betting Rs2,000 all the way down.
    "risk_pct_per_trade": 0.004,        # 0.4% = Rs2,000 at Rs5L, and it shrinks
                                        # automatically as equity falls.

    # Adaptive sizing (see learning.py). Scales the percentage above by what the
    # trade record supports believing, and by drawdown state. Hard-bounded to
    # [0.20%, 0.60%]. It learns from wins AND losses, but shrinks its belief
    # toward a prior by sample size, so a five-loss streak - which a genuinely
    # good 45%-win strategy produces about 5% of the time - barely moves it.
    #
    # This adapts SIZE ONLY. Entry rules, exit rules, filters and thresholds
    # never self-adjust; those change when a human reads analyse_trades.py and
    # decides. v1's parameters were all reactions to observed results, and it
    # lost Rs11,317 - automating that loop would only run it faster.
    #
    # Set False to return to flat 0.4% sizing.
    "adaptive_sizing": True,

    # Hard caps. v1 had none of these, which is why one trade could eat 73% of
    # the account and the next four became qty=1 fragments.
    # Raised from 2/3 once the universe went to 214 names. Measured signal rate
    # is ~3/day of candidates; with the old caps the bot topped out at 63 trades
    # a month, which is 6 months before any review has enough data to say
    # anything. These caps are what turn universe size into evidence.
    #
    # The SECTOR cap below is what makes this safe. v1's problem was never the
    # number of positions - it was that five NSE large-cap longs are one
    # leveraged NIFTY bet in five tickets. With 1 per sector and 23 sectors,
    # three concurrent positions are in three different sectors.
    "max_concurrent_positions": 3,      # v1: 5, v2 first cut: 2
    "max_entries_per_day": 5,           # v1: 5, v2 first cut: 3
    "max_positions_per_sector": 1,      # v1 had no sector limit at all
    "max_gross_notional_mult": 2.0,     # total exposure <= 2.0x equity (v1 ran 3.6x)
    "max_notional_per_trade_pct": 0.90, # one trade <= 90% of equity notional

    # The fragment killer, now DERIVED (see the bottom of this file) as a
    # fraction of target notional rather than a fixed Rs60,000. The intent was
    # never "Rs60,000" - it was "reject a position that got shrunk to a token
    # of what was intended". A fixed rupee floor stops meaning that the moment
    # capital changes: at Rs10L every position clears Rs60,000 trivially, and
    # the guard silently retires.
    #
    # v1 took trades as small as Rs1,264 notional (qty=1), where the ~Rs47 fixed
    # brokerage alone is 3.7% of the position. Those won 6.7% of the time.
    "min_notional_fraction_of_target": 0.25,
    "min_notional_per_trade": None,     # computed below

    # Never be more than 1% of a bar's liquidity - keeps the 5 bps slippage
    # assumption honest.
    "max_pct_of_bar_volume": 0.01,

    # Circuit breakers. v1 had zero. Its worst day was -Rs4,178 and its max
    # drawdown was Rs20,177 (4.0% of capital) in 21 trading days, with no rule
    # that would ever have stopped it.
    # Widened with the entry cap: at -2R the bot would halt after two losing
    # trades, which makes a 5-entry cap meaningless. -3R is 1.2% of equity on a
    # bad day. This IS a loosening, and it is the only one here - the drawdown
    # halt and kill switch are unchanged.
    "daily_loss_limit_R": 3.0,          # was 2.0
    "weekly_loss_limit_R": 8.0,         # was 6.0
    "max_drawdown_halt_pct": 0.06,      # halt entirely at -6% equity

    # Kill switch: if the last N closed trades have a profit factor below the
    # threshold, stop and require a manual restart. This is what catches a
    # regime change before it catches you.
    "killswitch_lookback_trades": 30,
    "killswitch_min_pf": 0.85,
}

# ---------------------------------------------------------------------------
# STRATEGY: VWAP mean reversion, cost-aware
# ---------------------------------------------------------------------------
STRATEGY = {
    # --- signal ---
    "rsi_period": 14,
    "rsi_oversold": 40,              # UNVALIDATED on 15m. v1 used 28, loosened
                                     # from a 25 that was tuned on HOURLY bars
                                     # and never re-swept for 15m.

    # Minimum distance below VWAP. NOT hand-picked - DERIVED from the cost
    # hurdle below, so the two filters can never drift apart.
    #
    # Let D = (VWAP - trigger) / trigger, s = the floor stop %, c = round-trip
    # cost %, k = the T2 overshoot. The blended target sits at
    # trigger + (1 + k/2)*D*trigger, so clearing the hurdle requires:
    #
    #     (1 + k/2)*D - c  >=  min_reward_risk_after_cost * s
    #     D  >=  (min_rr * s + c) / (1 + k/2)
    #
    # At min_rr 1.5, s 0.8%, c 0.1355%, k 0.5 that is 1.07% from the trigger.
    # The signal measures from the bar CLOSE and the trigger sits above it, so
    # add a buffer for that gap. Result: ~1.2%.
    #
    # MEASURED, 2026-09-24. Three days live produced ZERO signals. The reject
    # tally in signals_v2.jsonl showed why: of the 73 names that ever reached
    # this test, the largest deviation seen was 1.20% against a 1.218% floor.
    # The threshold sat just above the ceiling of what the market does.
    #
    # The cause was a 0.15% "buffer" I added on top of an already-conservative
    # derivation, to cover the gap between the bar close and the trigger. It is
    # redundant: the EXACT cost hurdle is evaluated a few lines later using the
    # real trigger, real ATR stop and real VWAP. A conservative approximation
    # sitting in front of an exact test can only reject trades the exact test
    # would have accepted. Buffer removed.
    #
    # This is not v1's mistake repeated. v1 loosened the threshold to fire more
    # often and left the economics broken. Here the economics - the cost hurdle
    # at 1.5 R:R after costs - are UNCHANGED. Only the cheap pre-filter that
    # was stricter than the real test has moved.
    "min_vwap_deviation": None,   # computed below, after min_rr is defined

    # --- the confirmation trigger (NEW, and the biggest logic change) ---
    # v1 bought at the close of the currently-forming bar while price was still
    # falling. That is (a) unfillable - you cannot transact at a price you only
    # learn when the bar closes - and (b) a falling-knife catch with no evidence
    # the fall has stopped.
    # v2 places a stop-limit BUY above the signal bar's high. Price has to turn
    # up and take you in. If it keeps falling, you are never filled and you lose
    # nothing. This converts the strategy from "predict the bottom" to "wait for
    # the bottom to prove itself".
    "require_reversal_trigger": True,
    "trigger_offset_ticks": 1,
    "trigger_valid_bars": 2,         # order stands for 2 bars, then cancels

    # --- stop ---
    # ATR-based, not a fixed percentage. A flat 0.55% is ~3 ATRs on ITC and
    # ~0.3 ATR on TATASTEEL: the same number means opposite things in different
    # names. It also forces a Rs3.64L notional, maximising cost per unit risk.
    "stop_atr_mult": 1.2,
    "atr_period": 14,
    "stop_pct_floor": 0.006,         # was 0.008 - see note

    # WHY THE FLOOR MOVED 0.8% -> 0.6%
    # It is a pass-through of the cost correction, not a loosening. The floor
    # exists to stop friction eating the trade, and the measure of that is
    # friction as a share of risk. The original design accepted 18.1% at a 0.8%
    # stop under the old (wrong) 0.1355% cost. Correcting entry slippage to
    # reflect the limit order drops cost to 0.0955%, and a 0.6% stop now carries
    # 17.1% - strictly better than what was signed off originally.
    #
    # This matters because the deviation floor is DERIVED from this number, and
    # the derived floor turned out to sit above what the market actually does.         # 0.80% - below this, costs eat the trade
    "stop_pct_cap": 0.022,           # 2.20% - above this, the setup is too wild
    "stop_below_signal_low": True,   # also respect structure: stop under the low

    # --- target & exits ---
    # Scale out at VWAP (the thesis completing), let the rest run.
    "t1_at_vwap": True,
    "t1_fraction": 0.5,
    "t2_vwap_overshoot": 0.5,        # T2 = VWAP + 0.5 x (entry->VWAP distance)
    "trail_atr_mult_after_t1": 1.2,

    # THE COST HURDLE. Reject any signal whose distance to VWAP is not at least
    # this multiple of the stop distance, after costs are added in price terms.
    # v1's geometry at threshold was 0.78% target vs 0.55% stop = 0.91:1 AFTER
    # costs, needing a 52.5% win rate to break even while running 32.7%.
    "min_reward_risk_after_cost": 1.5,

    # Cost-aware breakeven trail. v1 moved the stop to entry + 0.10R at +1R.
    # With friction at 0.27R that rule LOCKS IN A LOSS of -0.17R - and v1's one
    # trailing-stop exit duly lost Rs328. The trail must clear friction first.
    "trail_activate_R": 1.5,
    "trail_to_R": "friction + 0.15",  # computed at runtime from costs.py

    # Time stop. v1 had none, and 27 of its 49 trades (55%) died at square-off
    # with an average of -Rs137: capital tied up all afternoon for nothing.
    # If a mean-reversion trade has not started reverting within an hour, the
    # thesis is wrong - it is trending, not stretched.
    "time_stop_bars": 4,             # 4 x 15m = 60 min
    "time_stop_min_R": 0.4,          # must be at least +0.4R by then
}

# ---------------------------------------------------------------------------
# FILTERS - the "do not fade real information" layer
# ---------------------------------------------------------------------------
# v1 had no way to tell a random dip from a news dip. Mean reversion works on
# liquidity-driven noise and fails badly on information. These separate them.
FILTERS = {
    # Market regime. Buying dips in individual names while the whole index
    # slides is not mean reversion, it is fighting index flow with leverage.
    # v1 ran up to 5 correlated longs with no index awareness at all - on
    # 2026-09-15 it opened 5 positions and all 5 lost.
    "require_index_above_vwap": True,
    "index_vwap_tolerance": 0.003,   # allow NIFTY up to 0.3% below its VWAP
    "max_index_daily_drop": 0.010,   # no dip-buying if NIFTY is off >1.0%

    # Volatility regime. Mean reversion needs a calm tape; in a panic,
    # "oversold" keeps getting more oversold.
    "max_india_vix": 22.0,
    "min_india_vix": 9.0,            # dead-flat tape cannot pay the costs either

    # Relative volume. This is the news detector, and note the direction:
    # a breakout strategy WANTS high RVOL ("stocks in play"); a mean-reversion
    # strategy must AVOID it. High RVOL means information is arriving, and you
    # do not fade information.
    # MEASURED: the RVOL band was the single biggest filter, rejecting 41% of
    # all name-checks - and 79% of those rejections were for being too QUIET,
    # not for news. rvol_min was mine, justified in a comment as "too quiet for
    # reversion flow", which is a rationalisation rather than evidence. A stock
    # sitting below VWAP on ordinary volume is arguably a BETTER reversion
    # candidate: less information in the move, more of it liquidity noise.
    #
    # rvol_max stays. It has a real mechanism - do not fade news - and it
    # accounted for only 21% of the rejections.
    "rvol_min": 0.0,                 # was 0.7, removed on measurement
    "rvol_max": 2.0,                 # kept: above this, assume news and stand aside

    # Gap filter - an overnight gap is an event, not a stretched rubber band.
    "max_opening_gap_pct": 0.015,

    # Liquidity floor, DERIVED from capital (see the bottom of this file).
    # A fixed Rs2cr was already too low: at Rs5L the wanted notional was Rs2.5L
    # against a Rs2L cap (1% of a Rs2cr bar), so positions in thin names were
    # being silently shrunk below target risk - which concentrates the book into
    # liquid names without ever saying so.
    #
    # Deriving it means the universe self-selects to names that can absorb the
    # size actually being traded. Raise capital and the floor follows.
    "min_median_15m_turnover": None,    # computed below
}

# ---------------------------------------------------------------------------
# DERIVED PARAMETERS - kept here so they cannot silently disagree with the
# values they depend on. Change min_reward_risk_after_cost or stop_pct_floor
# and the deviation threshold follows automatically.
# ---------------------------------------------------------------------------
def _min_deviation_for_cost_hurdle(
    min_rr: float,
    stop_floor: float,
    t2_overshoot: float,
    cost_pct: float,
    trigger_gap: float = 0.0,
) -> float:
    """
    Smallest VWAP deviation that can clear the cost hurdle.

    trigger_gap defaults to 0. It was 0.0015, and that buffer is what made the
    strategy untradeable - it stacked a safety margin on top of a test that is
    re-run exactly, moments later, on the real numbers.
    """
    blended_mult = 1.0 + t2_overshoot / 2.0
    from_trigger = (min_rr * stop_floor + cost_pct) / blended_mult
    return round(from_trigger + trigger_gap, 5)


def _target_notional() -> float:
    """Notional a full-size trade wants, at the floor stop."""
    return SYSTEM["initial_capital"] * RISK["risk_pct_per_trade"] / STRATEGY["stop_pct_floor"]


# A name must be able to absorb the SMALLEST position we would accept, not a
# full-size one. Deriving from target notional asked "can this name take a
# maximum position at the tightest stop?" - the worst case on both axes - and
# rejected 35% of all name-checks at Rs5cr. The position sizer already scales
# down to fit max_pct_of_bar_volume, and min_notional_per_trade already refuses
# anything shrunk too far. The pre-filter was double-counting both.
RISK["min_median_15m_turnover"] = (
    _target_notional() * RISK["min_notional_fraction_of_target"]
    / RISK["max_pct_of_bar_volume"]
)
FILTERS["min_median_15m_turnover"] = RISK["min_median_15m_turnover"]

# A position shrunk below this fraction of intent is not a smaller good trade,
# it is a donation to the fixed costs.
RISK["min_notional_per_trade"] = _target_notional() * RISK["min_notional_fraction_of_target"]

STRATEGY["min_vwap_deviation"] = _min_deviation_for_cost_hurdle(
    min_rr=STRATEGY["min_reward_risk_after_cost"],
    stop_floor=STRATEGY["stop_pct_floor"],
    t2_overshoot=STRATEGY["t2_vwap_overshoot"],
    cost_pct=VARIABLE_ROUNDTRIP_PCT,
)

# ---------------------------------------------------------------------------
# MOMENTUM v3 - the strategy that replaced VWAP mean reversion
# ---------------------------------------------------------------------------
# WHY THE DIRECTION FLIPPED
#
# On 2026-09-24 the reversion premise was measured directly, with no trading
# machinery attached: 185,847 real 15-minute bars, forward returns from the
# entry condition, no stops, no sizing, no costs. Buying a stock stretched
# below VWAP with low RSI returned:
#
#       +15 min  +0.008%      +1 hour  -0.028%
#       +2 hours -0.085%      to close -0.137%   (t = -7.97, n = 4,546)
#
# It does not revert. It keeps falling, and it falls further the longer you
# hold. Being MORE oversold made it worse, not better (-0.095% at 0.8-1.2%
# below VWAP against -0.004% at 0.2-0.4%), which means the carefully derived
# deviation floor above was steering toward the worst available bucket. RSI
# bucketed from 0-20 through 40-50 was uniformly negative, so the oversold
# threshold was never the problem either.
#
# The mirror of that condition was the only positive result anywhere in the
# study, and four unrelated features all graded monotonically in the same
# direction (all figures net of the 0.0955% round-trip cost, held to the close,
# entered at a tradeable price - the next bar's open):
#
#       RSI         60-65 -0.126%   65-70 -0.026%   70-75 +0.017%  75+ +0.147%
#       rel volume  1.2-2 -0.050%   2-4   +0.014%   4+    +0.128%
#       dev >vwap   0.8-1.2 0.000%  1.2-1.8 +0.082%  1.8-3 +0.117%
#       ATR         0.4-0.6 -0.017% 0.6-0.9 +0.062%  0.9+  +0.318%
#
# One good bucket is luck. Four unrelated features each grading monotonically
# the same way is a relationship: the return concentrates in strong, heavily
# traded, volatile extensions. RSI>=75, rvol>=4 and dev>=1.2% each cleared cost
# in July, August AND September independently.
#
# WHAT THIS IS HONESTLY EXPECTED TO MAKE
#
# Close to nothing. Run as an account - 3 concurrent, 5 entries a day, real
# costs both sides - the same rule made +1.28% over 59 days at t = +0.20, and
# a 100-cell sweep over every threshold below found ZERO settings that were
# positive in all three months AND still positive with their best five trades
# removed. Best t across all 100 cells was +1.63, which is the maximum of 100
# tries on one period and therefore means nothing.
#
# So this is not deployed because it is expected to be profitable. It is
# deployed because it is the only signal measured on this market with the
# RIGHT SIGN, and running it forward in paper is the only honest way to find
# out. Every trade from here is genuine out-of-sample data; the 59 days above
# are now in-sample and spent. The go-live bar in README.md is unchanged.
#
# PARAMETERS ARE NOT THE SWEEP'S BEST CELL
#
# Deliberately. The best cell of a 100-cell search on one period is the most
# overfit number available. These come from where the monotone gradients above
# turn positive and hold across all three months - the same values would have
# been chosen from any of the three months alone.
MOMENTUM = {
    "rsi_period": 14,
    "atr_period": 14,

    # --- signal ---
    "min_rsi": 75,                  # the top RSI bucket; 70-75 was +0.017% net
    "min_dev_above_vwap": 0.012,    # 1.2%; the 0.8-1.2% band netted exactly 0.000%
    "min_rvol": 4.0,                # THIS BAR's volume vs the name's 50-bar median

    # NOT the cumulative-day RVOL that FILTERS uses. The measurement that
    # supports this number used bar-level volume, and the two are different
    # quantities - a name can be quiet all morning and print one enormous bar.
    # It is the enormous bar that carries the signal.
    "rvol_lookback_bars": 50,

    # Volatility floor. The ATR gradient was the steepest of the four
    # (+0.318% net above 0.9%), and the mechanism is plain: a name whose
    # 15-minute range is 0.4% cannot move far enough to pay 0.0955% of costs.
    "min_atr_pct": 0.005,

    # --- entry ---
    # A market order filled at the next bar's open. NO reversal trigger.
    #
    # v2's stop-limit-above-the-high trigger was mine, argued for at length,
    # and it is measurably harmful: -0.153% to the close against -0.137%
    # without it, and it fired on only 26% of setups. For a momentum entry it
    # is strictly worse than useless - it makes you pay up for the privilege of
    # confirming a move that was already confirmed.
    "order_type": "MARKET",

    # --- stop ---
    # Volatility-scaled with a floor, and the floor is WIDE on purpose.
    # notional = risk / stop%, so a wider stop means a smaller position and
    # less friction for the same rupee risk. Momentum wants that trade-off:
    # median adverse excursion before the close was -0.76%, so anything much
    # tighter than 1.2% is stopped out by ordinary noise.
    "stop_atr_mult": 2.0,
    "stop_pct_floor": 0.012,
    "stop_pct_cap": 0.025,

    # --- exits ---
    # Hold to square-off. No target, no partial, NO BREAKEVEN TRAIL.
    #
    # This looks negligent and it is the single most data-driven choice here.
    # The breakeven trail lost money in EVERY ONE of the 50 sweep cells it
    # appeared in, often catastrophically (-9.8% in the worst). The reason is
    # the return distribution: removing the best five trades from any cell of
    # the sweep turns it negative. The edge IS the right tail, and a breakeven
    # stop is a machine for amputating right tails. A time stop would do the
    # same thing more slowly.
    #
    # The cost is a 43% win rate and trades that give back open profit. That is
    # the shape of the distribution, not a flaw to be engineered away.
    "hold_to_square_off": True,
    "use_target": False,
    "use_breakeven_trail": False,
    "use_time_stop": False,

    # --- regime ---
    # NOT an alpha filter. Gating on "NIFTY above its VWAP" looked helpful
    # (+0.067% vs -0.024%) and failed in September, so it is not claimed as
    # edge. This is a solvency gate: with ~5x MIS leverage and three correlated
    # longs, a market in free-fall is the one condition that can do real damage
    # in a hurry. It stands down and says so.
    "max_index_daily_drop": 0.015,
    "max_india_vix": 28.0,
}

# ---------------------------------------------------------------------------
# WALK-FORWARD OVERRIDES
# ---------------------------------------------------------------------------
# walkforward.py may adjust four thresholds weekly, and only after the new
# values cleared cost on data the fit never saw. It writes params_live.json;
# this reads it.
#
# The clamp below is not defensive padding. It is the thing that makes the
# weekly job safe to run unattended: whatever that file contains - a bug, a
# half-written record, a value that scored beautifully on noise - it cannot
# move a threshold outside a band that was set by hand, and it cannot touch
# anything else. Direction, exits, risk per trade and the caps are not
# reachable from that file at all.
#
# A missing or unreadable file is normal and silent: the bot then runs the
# values above, which is exactly what it ran before the job existed.
_MOMENTUM_BANDS = {
    "min_dev_above_vwap": (0.008, 0.020),
    "min_rsi": (70, 80),
    "min_rvol": (2.0, 6.0),
    "stop_pct_floor": (0.010, 0.020),
}


def _apply_live_params():
    import json
    import os
    path = os.environ.get("ALGO_LIVE_PARAMS", "params_live.json")
    if not os.path.exists(path):
        return {}
    try:
        params = json.load(open(path)).get("params", {})
    except Exception:
        return {}
    applied = {}
    for key, (lo, hi) in _MOMENTUM_BANDS.items():
        if key not in params:
            continue
        try:
            v = float(params[key])
        except (TypeError, ValueError):
            continue
        if not (lo <= v <= hi):
            continue
        MOMENTUM[key] = int(v) if key == "min_rsi" else v
        applied[key] = MOMENTUM[key]
    return applied


LIVE_PARAM_OVERRIDES = _apply_live_params()

# Which strategy main.py runs. "momentum_v3" or "vwap_mr_v2".
# v2 is kept importable and testable rather than deleted: it is the control
# case, and the studies that condemned it must stay reproducible.
ACTIVE_STRATEGY = "momentum_v3"

REPORTING = {"report_dir": "reports", "log_dir": "logs"}

# ---------------------------------------------------------------------------
# BACK-COMPAT SHIMS - deprecated, kept only so the v1 modules and the research
# scripts in the repo root still import. Nothing in the live v2 path uses these.
#
# COSTS in particular is the OLD cost dict, and it is the reason this repo lost
# money: it was mirrored by a Rs50 flat fee in profit_max_sweep.py. Live code
# must import from costs.py instead, which is the single source of truth.
# ---------------------------------------------------------------------------
try:
    from config_v1 import (          # noqa: F401
        ORB_CONFIG,
        VWAP_MR_CONFIG,
        COSTS,
    )
except ImportError:                  # pragma: no cover
    ORB_CONFIG = VWAP_MR_CONFIG = COSTS = {}

# These two were referenced by strategies/ema_cross.py and vwap_pullback.py but
# never actually defined in v1's config.py either - those modules have been
# broken since before this rebuild. Empty dicts keep imports from exploding.
EMA_CROSS = {}
VWAP_PULLBACK = {}

# ---------------------------------------------------------------------------
# VALIDATION PROTOCOL - the rule that would have prevented all of this
# ---------------------------------------------------------------------------
VALIDATION = {
    "train_period": ("2023-08-01", "2024-12-31"),
    "validate_period": ("2025-01-01", "2025-12-31"),
    "test_period": ("2026-01-01", "2026-09-19"),   # touch ONCE, at the end
    "bar_interval": "15m",           # must match live. v1 tuned on 1h, ran 15m.
    "min_trades_for_significance": 200,
    "require_positive_after_costs": True,
    "max_configs_tested": 20,        # declare this; more configs = more overfit
}
