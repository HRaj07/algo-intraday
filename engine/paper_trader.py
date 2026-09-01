"""
Institutional Grade Paper Trading Engine
- Fixed Risk Allocation (₹2,000 per trade slot)
- Hard Daily Trade Limit (Max 2 trades/day)
- Dynamic Breakeven Trailing Stop (+1R reached -> Move SL to breakeven + 0.1R)
- Exact transaction fee & slippage calculation
- Automatic Square-off at 3:15 PM IST
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from config import SYSTEM, COSTS, ORB_CONFIG

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, state_file: str = "logs/paper_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> Dict:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "cash": SYSTEM["initial_capital"],
            "positions": {},
            "trade_history": [],
            "daily_trades_count": {},
            "total_pnl": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
        }

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def _calc_cost(self, price: float, qty: int, side: str) -> float:
        """Calculate NSE intraday transaction cost + slippage."""
        value = price * qty
        brokerage = COSTS["brokerage_per_order"] * (1 + COSTS["gst_on_brokerage"])
        stt = value * COSTS["stt_pct"] if side == "sell" else 0
        exchange = value * COSTS["exchange_charges_pct"]
        sebi = value * COSTS["sebi_charges_pct"]
        slippage = value * COSTS["slippage_pct"]
        return brokerage + stt + exchange + sebi + slippage

    def enter_trade(self, signal: Dict) -> Optional[Dict]:
        """Open a position with risk-based sizing and daily trade limits."""
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        ticker = signal["ticker"]
        now_ist = datetime.now(ist)
        today_str = str(now_ist.date())
        
        # Check daily trade count
        trades_today = self.state.get("daily_trades_count", {}).get(today_str, 0)
        if trades_today >= SYSTEM["max_daily_trades"]:
            logger.info(f"Daily trade limit ({SYSTEM['max_daily_trades']}) reached for {today_str}")
            return None

        if ticker in self.state["positions"]:
            return None  # Already in this ticker

        price = signal["price"]
        stop_loss = signal["stop_loss"]
        risk_per_share = abs(price - stop_loss)
        if risk_per_share <= 0:
            return None

        # Fixed Risk Sizing: Qty = Risk Amount / Risk Per Share
        target_risk = SYSTEM["risk_per_trade"]
        qty = max(1, int(target_risk / risk_per_share))

        entry_cost = self._calc_cost(price, qty, "buy")
        required_capital = price * qty + entry_cost

        if required_capital > self.state["cash"]:
            qty = max(1, int((self.state["cash"] - 100) / price))
            if qty < 1:
                return None
            entry_cost = self._calc_cost(price, qty, "buy")
            required_capital = price * qty + entry_cost

        self.state["cash"] -= required_capital
        direction = "LONG" if signal["signal"] == "BUY" else "SHORT"

        position = {
            "ticker": ticker,
            "direction": direction,
            "entry_price": price,
            "qty": qty,
            "stop_loss": stop_loss,
            "initial_stop_loss": stop_loss,
            "target": signal["target"],
            "risk_per_share": round(risk_per_share, 2),
            "trailed_to_be": False,
            "strategy": signal["strategy"],
            "entry_time": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
            "entry_cost": round(entry_cost, 2),
        }

        self.state["positions"][ticker] = position
        self.state["total_trades"] += 1
        
        if "daily_trades_count" not in self.state:
            self.state["daily_trades_count"] = {}
        self.state["daily_trades_count"][today_str] = trades_today + 1
        
        self._save_state()
        logger.info(
            f"ENTER {direction} {ticker} @ ₹{price} | Qty: {qty} | "
            f"SL: ₹{stop_loss} | Target: ₹{signal['target']} | Risk: ₹{round(risk_per_share * qty, 0)}"
        )
        return position

    def check_exits(self, current_data: Dict[str, Dict[str, float]]) -> List[Dict]:
        """
        Check stops, targets, breakeven trailing logic, and square-off.
        current_data is expected to be {ticker: {'close': c, 'high': h, 'low': l}}
        """
        exits = []
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist)
        force_exit = now.hour > 15 or (now.hour == 15 and now.minute >= 15)

        for ticker, pos in list(self.state["positions"].items()):
            bar_data = current_data.get(ticker)
            if not bar_data:
                continue

            price = bar_data['close']
            high = bar_data.get('high', price)
            low = bar_data.get('low', price)

            direction = pos["direction"]
            entry_price = pos["entry_price"]
            curr_stop = pos["stop_loss"]
            target = pos["target"]
            risk = pos["risk_per_share"]
            exit_reason = None
            exit_price = price

            # Dynamic Breakeven Trailing Stop: If price reaches +1R, move stop to Entry + 0.1R
            if not pos.get("trailed_to_be", False):
                if direction == "LONG" and high >= entry_price + risk:
                    pos["stop_loss"] = round(entry_price + ORB_CONFIG["trail_buffer"] * risk, 2)
                    pos["trailed_to_be"] = True
                    logger.info(f"TRAIL {ticker} stop moved to breakeven + buffer: ₹{pos['stop_loss']}")
                    self._save_state()
                elif direction == "SHORT" and low <= entry_price - risk:
                    pos["stop_loss"] = round(entry_price - ORB_CONFIG["trail_buffer"] * risk, 2)
                    pos["trailed_to_be"] = True
                    logger.info(f"TRAIL {ticker} stop moved to breakeven + buffer: ₹{pos['stop_loss']}")
                    self._save_state()

            curr_stop = pos["stop_loss"]

            if force_exit:
                exit_reason = "square_off"
                exit_price = price
            elif direction == "LONG":
                if low <= curr_stop:
                    exit_reason = "trailing_stop" if pos.get("trailed_to_be") else "stop_loss"
                    exit_price = curr_stop
                elif high >= target:
                    exit_reason = "target"
                    exit_price = target
            elif direction == "SHORT":
                if high >= curr_stop:
                    exit_reason = "trailing_stop" if pos.get("trailed_to_be") else "stop_loss"
                    exit_price = curr_stop
                elif low <= target:
                    exit_reason = "target"
                    exit_price = target

            if exit_reason:
                exit_record = self._close_position(ticker, pos, exit_price, exit_reason)
                exits.append(exit_record)

        return exits

    def _close_position(self, ticker: str, pos: Dict, exit_price: float, reason: str) -> Dict:
        """Close position and update ledger."""
        qty = pos["qty"]
        entry_price = pos["entry_price"]
        direction = pos["direction"]
        exit_cost = self._calc_cost(exit_price, qty, "sell")

        if direction == "LONG":
            pnl = (exit_price - entry_price) * qty - pos["entry_cost"] - exit_cost
        else:
            pnl = (entry_price - exit_price) * qty - pos["entry_cost"] - exit_cost

        proceeds = exit_price * qty - exit_cost
        self.state["cash"] += proceeds
        self.state["total_pnl"] += pnl
        if pnl > 0:
            self.state["winning_trades"] += 1

        trade_record = {
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": round(exit_price, 2),
            "qty": qty,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (entry_price * qty) * 100, 2),
            "reason": reason,
            "strategy": pos["strategy"],
            "entry_time": pos["entry_time"],
            "exit_time": datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST"),
        }
        self.state["trade_history"].append(trade_record)
        del self.state["positions"][ticker]
        self._save_state()
        logger.info(f"EXIT {ticker} @ ₹{exit_price} | P&L: ₹{pnl:+.0f} ({reason})")
        return trade_record

    def get_summary(self) -> Dict:
        total_trades = self.state["total_trades"]
        winning = self.state["winning_trades"]
        win_rate = (winning / total_trades * 100) if total_trades > 0 else 0
        return {
            "cash": round(self.state["cash"], 2),
            "open_positions": len(self.state["positions"]),
            "total_pnl": round(self.state["total_pnl"], 2),
            "total_trades": total_trades,
            "win_rate_pct": round(win_rate, 1),
            "initial_capital": SYSTEM["initial_capital"],
            "return_pct": round(self.state["total_pnl"] / SYSTEM["initial_capital"] * 100, 2),
        }
