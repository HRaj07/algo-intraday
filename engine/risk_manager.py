"""
Risk manager - position sizing, exposure caps and circuit breakers.

This module exists because v1 had no concept of a capital constraint. Its
sizing rule was:

    qty = risk_rupees / risk_per_share          # then deduct price*qty from cash

With Rs2,000 risk and a 0.55% stop that is Rs3.64 LAKH of notional on a Rs5 lakh
account - trade #1 consumed 73% of the book. Trades #2-#5 then silently fell
through to `qty = (cash - 100) / price`, which produced positions as small as
qty=1. Observed result across 49 paper trades:

    slot #1 of the day : median notional Rs2,85,726, net -Rs11,780  (the entire loss)
    slot #4 of the day : median notional Rs2,701,    net -Rs103

Meanwhile the backtest that chose "max 5 trades/day" modelled no cash constraint
at all, so it happily took five full-size trades a day that the account could
never have funded. The live system and the backtest were not running the same
strategy.

Everything here is a hard gate: size_position() returns None rather than
returning a broken position, and a rejected trade is logged with its reason.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from config import RISK, SECTOR
from costs import cost_in_rupees, one_side_cost

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, state: Dict):
        self.state = state

    # ------------------------------------------------------------------ equity
    def equity(self) -> float:
        """
        Account value: cash + margin held against open positions + unrealised P&L.

        THIS WAS BADLY WRONG IN THE FIRST v2 CUT and the bug was severe enough to
        brick the bot. The original was:

            cash + sum(entry_price * qty)

        but opening a position only deducts the 20% MIS margin from cash, so that
        expression added the *full* notional on top of cash that still held 80% of
        it. A Rs3L position inflated reported equity by Rs2.4L.

        The damage: equity_peak latched onto the inflated figure, and the moment
        the position closed, equity "fell" back to reality. The drawdown halt read
        that as a 30-65% loss and permanently stopped trading - after the first
        completed trade. It also inflated risk_budget() and the gross-notional
        headroom while any position was open, so each subsequent trade was sized
        off a fantasy account balance.

        Caught by a stray test-harness log, not by the test suite, which asserted
        plenty about exits and sizing but never that equity stayed sane across a
        position's life. test_pipeline.py now checks exactly that.
        """
        cash = self.state.get("cash", 0.0)
        positions = self.state.get("positions", {})
        prices = self.state.get("last_known_price", {})

        margin_held = 0.0
        unrealised = 0.0
        for ticker, p in positions.items():
            margin_held += p.get("margin", 0.0)
            qty_open = p.get("qty_open", p.get("qty", 0))
            last = prices.get(ticker, {}).get("close")
            if last:
                unrealised += (last - p["entry_price"]) * qty_open

        return cash + margin_held + unrealised

    def risk_budget(self) -> float:
        """
        Rupee risk for one trade.

        With adaptive sizing on, the percentage itself moves: down during
        drawdowns, and up or down with what the trade record supports believing
        about expectancy. See learning.py - the belief is shrunk toward a prior
        by sample size, so ten bad trades barely move it and two hundred do.

        Entry and exit rules never adapt. Only size.
        """
        eq = self.equity()
        if not RISK.get("adaptive_sizing", False):
            return eq * RISK["risk_pct_per_trade"]

        from learning import adaptive_risk_pct
        pct, detail = adaptive_risk_pct(
            self.state.get("trade_history", []),
            eq,
            self.state.get("equity_peak", eq),
            base=RISK["risk_pct_per_trade"],
        )
        self.state["last_sizing_decision"] = detail   # auditable after the fact
        return eq * pct

    def gross_notional(self) -> float:
        return sum(
            p["entry_price"] * p["qty"] for p in self.state.get("positions", {}).values()
        )

    # ------------------------------------------------------- circuit breakers
    def trading_halted(self, now: datetime) -> Tuple[bool, str]:
        """
        Returns (halted, reason). Checked before every entry.
        v1 had none of these checks; nothing would ever have stopped it.
        """
        eq = self.equity()
        start = self.state.get("initial_capital", eq)

        # 1. Absolute drawdown halt
        peak = self.state.get("equity_peak", start)
        if peak > 0 and (peak - eq) / peak >= RISK["max_drawdown_halt_pct"]:
            return True, (
                f"drawdown {(peak - eq) / peak * 100:.1f}% >= "
                f"{RISK['max_drawdown_halt_pct'] * 100:.0f}% - manual restart required"
            )

        # 2. Daily loss limit
        R = self.risk_budget()
        today_pnl = self._pnl_since(now.date())
        if today_pnl <= -RISK["daily_loss_limit_R"] * R:
            return True, (
                f"daily loss Rs{today_pnl:,.0f} hit the "
                f"-{RISK['daily_loss_limit_R']}R limit (-Rs{RISK['daily_loss_limit_R'] * R:,.0f})"
            )

        # 3. Weekly loss limit
        week_start = now.date() - timedelta(days=now.weekday())
        week_pnl = self._pnl_since(week_start)
        if week_pnl <= -RISK["weekly_loss_limit_R"] * R:
            return True, f"weekly loss Rs{week_pnl:,.0f} hit the -{RISK['weekly_loss_limit_R']}R limit"

        # 4. Rolling profit-factor kill switch
        hist = self.state.get("trade_history", [])
        n = RISK["killswitch_lookback_trades"]
        if len(hist) >= n:
            recent = hist[-n:]
            gp = sum(t["pnl"] for t in recent if t["pnl"] > 0)
            gl = abs(sum(t["pnl"] for t in recent if t["pnl"] <= 0))
            pf = gp / gl if gl > 0 else 99.0
            if pf < RISK["killswitch_min_pf"]:
                return True, (
                    f"rolling {n}-trade PF {pf:.2f} < {RISK['killswitch_min_pf']} "
                    f"- edge has degraded, manual restart required"
                )

        return False, ""

    def _pnl_since(self, since_date) -> float:
        total = 0.0
        for t in self.state.get("trade_history", []):
            try:
                d = datetime.strptime(t["exit_time"][:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if d >= since_date:
                total += t["pnl"]
        return total

    # ------------------------------------------------------- portfolio gates
    def can_open(self, ticker: str, now: datetime) -> Tuple[bool, str]:
        positions = self.state.get("positions", {})

        if ticker in positions:
            return False, "already holding this name"

        if len(positions) >= RISK["max_concurrent_positions"]:
            return False, f"at max concurrent positions ({RISK['max_concurrent_positions']})"

        today = str(now.date())
        if self.state.get("daily_entry_count", {}).get(today, 0) >= RISK["max_entries_per_day"]:
            return False, f"at max entries per day ({RISK['max_entries_per_day']})"

        # Sector concentration. Two bank longs is one bank trade in two tickets;
        # v1's book was routinely several correlated NSE large-cap longs at once,
        # which is a single leveraged NIFTY bet wearing five hats.
        sector = SECTOR.get(ticker, "OTHER")
        held = sum(1 for t in positions if SECTOR.get(t, "OTHER") == sector)
        if held >= RISK["max_positions_per_sector"]:
            return False, f"already hold {held} position(s) in {sector}"

        halted, why = self.trading_halted(now)
        if halted:
            return False, f"HALTED: {why}"

        return True, ""

    # --------------------------------------------------------------- sizing
    def size_position(
        self,
        entry_price: float,
        stop_price: float,
        bar_volume: Optional[float] = None,
    ) -> Tuple[Optional[int], str]:
        """
        Returns (qty, reason). qty is None when the trade must be skipped.

        Skipping is a feature. v1's fallback - shrink the position to whatever
        cash is lying around - turned 15 of its 49 trades into fragments that
        won 6.7% of the time, because at qty=1 the fixed brokerage alone swamps
        any move the strategy could capture.
        """
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return None, "non-positive risk per share"

        eq = self.equity()
        budget = self.risk_budget()
        qty = int(budget / risk_per_share)
        if qty < 1:
            return None, "risk budget cannot fund a single share"

        # Cap 1: single-trade notional
        max_trade_notional = eq * RISK["max_notional_per_trade_pct"]
        qty = min(qty, int(max_trade_notional / entry_price))

        # Cap 2: total gross exposure
        room = eq * RISK["max_gross_notional_mult"] - self.gross_notional()
        if room <= 0:
            return None, "no gross-exposure room left"
        qty = min(qty, int(room / entry_price))

        # Cap 3: never exceed ~1% of bar liquidity, or the slippage model breaks
        if bar_volume and bar_volume > 0:
            qty = min(qty, int(bar_volume * RISK["max_pct_of_bar_volume"]))

        # Cap 4: cash must actually cover margin. MIS gives ~5x on NSE equity,
        # so margin is ~20% of notional - but we also refuse to go past the
        # gross cap above, so this is a backstop rather than the binding limit.
        margin_needed = entry_price * qty * 0.20
        if margin_needed > self.state.get("cash", 0):
            qty = int((self.state.get("cash", 0) / 0.20) / entry_price)

        if qty < 1:
            return None, "capped below one share"

        notional = entry_price * qty
        if notional < RISK["min_notional_per_trade"]:
            return None, (
                f"notional Rs{notional:,.0f} below the Rs{RISK['min_notional_per_trade']:,} "
                f"floor - fixed costs would dominate, skipping rather than trading small"
            )

        # Final sanity: friction must not be a silly share of the risk ACTUALLY
        # taken - not the risk we set out to take.
        #
        # This compared against `budget` until 2026-09-24, and that was wrong in
        # a way only cheaper costs made reachable. The caps above can shrink qty
        # far below the risk budget; comparing friction to the original budget
        # then flatters a position whose real risk has collapsed. A trade risking
        # Rs450 to pay Rs477 of friction passed a guard written to stop exactly
        # that, because it was measured against a Rs2,000 intention.
        friction = cost_in_rupees(notional)
        actual_risk = risk_per_share * qty
        if friction > 0.25 * actual_risk:
            return None, (
                f"friction Rs{friction:,.0f} is {friction / actual_risk * 100:.0f}% of the "
                f"Rs{actual_risk:,.0f} actually at risk (budget was Rs{budget:,.0f}) - "
                f"the caps shrank this position past the point of being worth taking"
            )

        return qty, f"qty={qty} notional=Rs{notional:,.0f} friction=Rs{friction:,.0f}"

    # ------------------------------------------------------------ bookkeeping
    def update_peak(self):
        eq = self.equity()
        self.state["equity_peak"] = max(self.state.get("equity_peak", eq), eq)
