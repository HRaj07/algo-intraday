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
# UNIVERSE
# ---------------------------------------------------------------------------
# Cut from 40 names to 25. The 40-name list was built to generate "more signal
# opportunities" - the wrong objective. More marginal signals on a fixed capital
# base means more fragment-sized trades, and fragments are pure cost.
#
# The filter is liquidity, because the 5 bps slippage assumption only holds in
# names where a Rs1.3L order is a rounding error. Names dropped from v1 for
# thin 15-min turnover or event-driven behaviour: ADANIENT, TRENT, SHRIRAMFIN,
# BAJAJ-AUTO, EICHERMOT, HEROMOTOCO, DIVISLAB, BPCL, ULTRACEMCO, POWERGRID.
# Four of those five were among v1's five worst P&L contributors.
INTRADAY_UNIVERSE = [
    # Banking & Financials
    "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS",
    "BAJFINANCE.NS", "INDUSINDBK.NS",
    # IT
    "TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS",
    # Large-cap diversified
    "RELIANCE.NS", "LT.NS", "BHARTIARTL.NS", "ITC.NS",
    # Pharma
    "SUNPHARMA.NS", "CIPLA.NS", "DRREDDY.NS",
    # Auto
    "MARUTI.NS", "M&M.NS",
    # Energy & Metals
    "NTPC.NS", "ONGC.NS", "TATASTEEL.NS", "JSWSTEEL.NS",
    # Consumer
    "TITAN.NS",
]

# Sector map - used to stop the book concentrating in one factor.
SECTOR = {
    "HDFCBANK.NS": "BANK", "ICICIBANK.NS": "BANK", "SBIN.NS": "BANK",
    "AXISBANK.NS": "BANK", "KOTAKBANK.NS": "BANK", "BAJFINANCE.NS": "BANK",
    "INDUSINDBK.NS": "BANK",
    "TCS.NS": "IT", "INFY.NS": "IT", "HCLTECH.NS": "IT", "WIPRO.NS": "IT",
    "RELIANCE.NS": "DIVERSIFIED", "LT.NS": "DIVERSIFIED",
    "BHARTIARTL.NS": "DIVERSIFIED", "ITC.NS": "DIVERSIFIED",
    "SUNPHARMA.NS": "PHARMA", "CIPLA.NS": "PHARMA", "DRREDDY.NS": "PHARMA",
    "MARUTI.NS": "AUTO", "M&M.NS": "AUTO",
    "NTPC.NS": "ENERGY", "ONGC.NS": "ENERGY",
    "TATASTEEL.NS": "METAL", "JSWSTEEL.NS": "METAL",
    "TITAN.NS": "CONSUMER",
}

INDEX_TICKER = "^NSEI"      # NIFTY 50 - drives the regime gate
VIX_TICKER = "^INDIAVIX"

# ---------------------------------------------------------------------------
# SYSTEM
# ---------------------------------------------------------------------------
SYSTEM = {
    "mode": "paper",
    "initial_capital": 500_000,
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

    # Hard caps. v1 had none of these, which is why one trade could eat 73% of
    # the account and the next four became qty=1 fragments.
    "max_concurrent_positions": 2,      # was 5
    "max_entries_per_day": 3,           # was 5
    "max_positions_per_sector": 1,      # v1 had no sector limit at all
    "max_gross_notional_mult": 2.0,     # total exposure <= 2.0x equity (v1 ran 3.6x)
    "max_notional_per_trade_pct": 0.90, # one trade <= 90% of equity notional

    # The fragment killer. v1 took trades as small as Rs1,264 notional (qty=1),
    # where the ~Rs47 fixed brokerage alone is 3.7% of the position. Those
    # trades won 6.7% of the time. If you cannot fund a real position, skip it.
    "min_notional_per_trade": 60_000,

    # Never be more than 1% of a bar's liquidity - keeps the 5 bps slippage
    # assumption honest.
    "max_pct_of_bar_volume": 0.01,

    # Circuit breakers. v1 had zero. Its worst day was -Rs4,178 and its max
    # drawdown was Rs20,177 (4.0% of capital) in 21 trading days, with no rule
    # that would ever have stopped it.
    "daily_loss_limit_R": 2.0,          # stop trading for the day at -2R
    "weekly_loss_limit_R": 6.0,
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
    "rsi_oversold": 30,              # UNVALIDATED on 15m. v1 used 28, loosened
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
    # v1 used 0.6%, loosened from 0.8% to "fire on 15min bars". That generated
    # plenty of signals that could never pay for themselves - which is precisely
    # how it lost money. Signal count is not the objective.
    #
    # EXPECT FAR FEWER TRADES. If NSE large caps rarely dislocate 1.2% from
    # VWAP intraday, this strategy rarely has an opportunity worth taking. That
    # is a finding about the market, not a parameter to tune away.
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
    "stop_pct_floor": 0.008,         # 0.80% - below this, costs eat the trade
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
    "rvol_min": 0.7,                 # too quiet -> no reversion flow either
    "rvol_max": 2.0,                 # above this, assume news and stand aside

    # Gap filter - an overnight gap is an event, not a stretched rubber band.
    "max_opening_gap_pct": 0.015,

    # Liquidity floor, enforced per-name at scan time.
    "min_median_15m_turnover": 2_00_00_000,   # Rs2 crore per 15-min bar
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
    trigger_gap: float = 0.0015,
) -> float:
    """Smallest VWAP deviation that can clear the cost hurdle. See the note above."""
    blended_mult = 1.0 + t2_overshoot / 2.0
    from_trigger = (min_rr * stop_floor + cost_pct) / blended_mult
    return round(from_trigger + trigger_gap, 5)


STRATEGY["min_vwap_deviation"] = _min_deviation_for_cost_hurdle(
    min_rr=STRATEGY["min_reward_risk_after_cost"],
    stop_floor=STRATEGY["stop_pct_floor"],
    t2_overshoot=STRATEGY["t2_vwap_overshoot"],
    cost_pct=VARIABLE_ROUNDTRIP_PCT,
)

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
