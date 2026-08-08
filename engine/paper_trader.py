"""
Paper Trading Engine
Simulates intraday trades, tracks P&L and positions
"""
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
from config import SYSTEM, COSTS

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, state_file: str = "logs/paper_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> Dict:
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {
            "cash": SYSTEM["initial_capital"],
            "positions": {},
            "trade_history": [],
            "daily_pnl": [],
            "total_pnl": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
        }

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def _calc_cost(self, price: float, qty: int, side: str) -> float:
        """Calculate realistic transaction cost."""
        value = price * qty
        brokerage = COSTS["brokerage_per_order"] * (1 + COSTS["gst_on_brokerage"])
        stt = value * COSTS["stt_pct"] if side == "sell" else 0
        exchange = value * COSTS["exchange_charges_pct"]
        sebi = value * COSTS["sebi_charges_pct"]
        slippage = value * COSTS["slippage_pct"]
        return brokerage + stt + exchange + sebi + slippage

    def enter_trade(self, signal: Dict) -> Optional[Dict]:
        """Open a new paper position."""
        ticker = signal["ticker"]
        if ticker in self.state["positions"]:
            return None  # Already in this stock

        price = signal["price"]
        capital_to_use = self.state["cash"] * 0.15  # 15% of current cash
        qty = max(1, int(capital_to_use / price))
        cost = self._calc_cost(price, qty, "buy")
        total_cost = price * qty + cost

        if total_cost > self.state["cash"]:
            return None  # Not enough capital

        self.state["cash"] -= total_cost
        position = {
            "ticker": ticker,
            "direction": "LONG" if signal["signal"] == "BUY" else "SHORT",
            "entry_price": price,
            "qty": qty,
            "stop_loss": signal["stop_loss"],
            "target": signal["target"],
            "strategy": signal["strategy"],
            "entry_time": str(datetime.now()),
            "entry_cost": round(cost, 2),
        }
        self.state["positions"][ticker] = position
        self.state["total_trades"] += 1
        self._save_state()
        logger.info(f"ENTER {position['direction']} {ticker} @ ₹{price} qty={qty} | stop=₹{signal['stop_loss']} target=₹{signal['target']}")
        return position

    def check_exits(self, current_prices: Dict[str, float]) -> List[Dict]:
        """Check if any positions hit stop loss, target, or square-off time."""
        exits = []
        now = datetime.now()
        force_exit = now.hour > 15 or (now.hour == 15 and now.minute >= 15)

        for ticker, pos in list(self.state["positions"].items()):
            price = current_prices.get(ticker)
            if price is None:
                continue

            direction = pos["direction"]
            stop = pos["stop_loss"]
            target = pos["target"]
            exit_reason = None

            if force_exit:
                exit_reason = "square_off"
            elif direction == "LONG":
                if price <= stop:
                    exit_reason = "stop_loss"
                elif price >= target:
                    exit_reason = "target"
            elif direction == "SHORT":
                if price >= stop:
                    exit_reason = "stop_loss"
                elif price <= target:
                    exit_reason = "target"

            if exit_reason:
                exit_data = self._close_position(ticker, pos, price, exit_reason)
                exits.append(exit_data)

        return exits

    def _close_position(self, ticker: str, pos: Dict, exit_price: float, reason: str) -> Dict:
        """Close a position and record P&L."""
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
            "exit_time": str(datetime.now()),
        }
        self.state["trade_history"].append(trade_record)
        del self.state["positions"][ticker]
        self._save_state()
        logger.info(f"EXIT {ticker} @ ₹{exit_price} | P&L: ₹{pnl:.0f} ({reason})")
        return trade_record

    def get_summary(self) -> Dict:
        """Get current portfolio summary."""
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

    def square_off_all(self, current_prices: Dict[str, float]):
        """Force close all open positions (end of day)."""
        for ticker in list(self.state["positions"].keys()):
            if ticker in current_prices:
                self._close_position(ticker, self.state["positions"][ticker], current_prices[ticker], "eod_squareoff")
        self._save_state()
