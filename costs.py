"""
Single source of truth for NSE equity-intraday transaction costs.

WHY THIS FILE EXISTS
--------------------
The old system had two different cost models:
  - engine/paper_trader.py  -> scaled costs to notional (roughly correct)
  - profit_max_sweep.py     -> flat Rs50 per round trip (wrong by ~7x)

Every parameter in the old config.py was chosen by the Rs50 sweep. Re-pricing
that sweep's 540 trades at the real rate turns its advertised +Rs1,44,564 into
roughly -Rs56,000. The "edge" lived entirely inside the cost error.

Rule from here on: the backtester and the live engine import cost numbers from
THIS module and nowhere else. If they ever disagree again, the backtest is lying.

Rates verified against https://zerodha.com/charges/ (NSE equity intraday, 2026).
"""

# ---------------------------------------------------------------- statutory
BROKERAGE_PCT        = 0.0003      # 0.03% or Rs20 per executed order,
BROKERAGE_CAP        = 20.0        #   whichever is LOWER
STT_PCT_SELL         = 0.00025     # 0.025%, sell side only
EXCHANGE_TXN_PCT     = 0.0000307   # NSE 0.00307%, both sides
SEBI_PCT             = 0.000001    # Rs10 per crore, both sides
STAMP_DUTY_PCT_BUY   = 0.00003     # 0.003%, buy side only  (was MISSING in v1)
GST_PCT              = 0.18        # on brokerage + SEBI + exchange txn charges
                                   #   (v1 applied GST to brokerage only)

# ---------------------------------------------------------------- execution
# Slippage is an ASSUMPTION, not a fee. It is the single largest and single most
# controllable line item. 5 bps/side is a fair estimate for a market order in an
# NSE large cap. See SLIPPAGE_NOTES at the bottom for how to cut it.
SLIPPAGE_PCT_PER_SIDE = 0.0005     # 0.05%


def one_side_cost(price: float, qty: int, side: str) -> float:
    """Exact cost of one leg. `side` is 'buy' or 'sell'."""
    value = price * qty
    brokerage = min(value * BROKERAGE_PCT, BROKERAGE_CAP)
    exchange = value * EXCHANGE_TXN_PCT
    sebi = value * SEBI_PCT
    gst = (brokerage + exchange + sebi) * GST_PCT
    stt = value * STT_PCT_SELL if side == "sell" else 0.0
    stamp = value * STAMP_DUTY_PCT_BUY if side == "buy" else 0.0
    slippage = value * SLIPPAGE_PCT_PER_SIDE
    return brokerage + exchange + sebi + gst + stt + stamp + slippage


def round_trip_cost(entry_price: float, exit_price: float, qty: int) -> float:
    """Full cost of opening and closing one position."""
    return one_side_cost(entry_price, qty, "buy") + one_side_cost(exit_price, qty, "sell")


# Linearised round-trip rate, used for pre-trade filtering where qty is not yet
# known. Accurate to well within a rupee for any notional above ~Rs70,000.
VARIABLE_ROUNDTRIP_PCT = (
    STT_PCT_SELL
    + EXCHANGE_TXN_PCT * 2 * (1 + GST_PCT)
    + SEBI_PCT * 2 * (1 + GST_PCT)
    + STAMP_DUTY_PCT_BUY
    + SLIPPAGE_PCT_PER_SIDE * 2
)                                              # ~0.1343% of notional
FIXED_ROUNDTRIP = BROKERAGE_CAP * (1 + GST_PCT) * 2   # ~Rs47


def cost_in_rupees(notional: float) -> float:
    """Estimated round-trip cost for a given notional, before qty is fixed."""
    return notional * VARIABLE_ROUNDTRIP_PCT + FIXED_ROUNDTRIP


def cost_as_fraction_of_risk(risk_rupees: float, stop_pct: float) -> float:
    """
    THE MOST IMPORTANT FUNCTION IN THIS FILE.

    Position notional is risk_rupees / stop_pct. So a TIGHTER stop means a
    LARGER position and therefore MORE cost for the exact same rupee risk:

        stop 0.55%  ->  Rs3.64L notional  ->  Rs535 friction  =  27% of a Rs2,000 risk
        stop 1.50%  ->  Rs1.33L notional  ->  Rs226 friction  =  11% of a Rs2,000 risk
        stop 2.50%  ->  Rs0.80L notional  ->  Rs155 friction  =   8% of a Rs2,000 risk

    v1's config described its 0.55% stop as "Ultra Tight Stop, Max Capital
    Efficiency". It is the exact opposite: among all stop widths it is the one
    that maximises cost per unit of risk taken.
    """
    notional = risk_rupees / stop_pct
    return cost_in_rupees(notional) / risk_rupees


def breakeven_win_rate(risk_rupees: float, stop_pct: float, gross_rr: float) -> float:
    """Win rate needed to break even, once costs are charged to both outcomes."""
    friction = cost_in_rupees(risk_rupees / stop_pct)
    net_win = gross_rr * risk_rupees - friction
    net_loss = risk_rupees + friction
    if net_win <= 0:
        return 1.0
    return net_loss / (net_win + net_loss)


SLIPPAGE_NOTES = """
Slippage is 0.001% of notional x 100 = the biggest single cost, and it is the
only one you can negotiate. Three levers, in order of effect:

1. TRADE A SMALLER NOTIONAL FOR THE SAME RISK. Widening the stop from 0.55% to
   1.5% cuts notional by 63% and cuts slippage by the same 63%. This costs you
   nothing in risk terms. It is free money and it is the first thing to fix.

2. USE LIMIT ORDERS ON ENTRY. A mean-reversion entry is a liquidity-PROVIDING
   trade: you are buying a dip that sellers are hitting. Resting a limit at or
   just inside the bid can take entry slippage to roughly zero, sometimes
   negative. The cost is that some signals go unfilled - which is acceptable,
   because an unfilled dip-buy is usually one that kept falling.
   Exits (stop losses) must stay market orders. Never negotiate an exit.

3. SIZE AGAINST BAR VOLUME. Keep the order under ~1% of the stock's median
   15-minute rupee volume. Above that, the 5 bps assumption stops holding and
   you start paying real impact on top of spread.
"""
