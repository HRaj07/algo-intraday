"""
VWAP Pullback + Supertrend Strategy
Win Rate: ~58% with Supertrend filter | Source: AlgoTest.in, Sahi.com
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd
from data.fetcher import TechnicalIndicators
from config import VWAP_PULLBACK

logger = logging.getLogger(__name__)


class VWAPPullbackStrategy:
    def __init__(self):
        self.params = VWAP_PULLBACK
        self.ti = TechnicalIndicators()
        self.name = "VWAP_Pullback"

    def compute_signals(self, data_dict: Dict[str, pd.DataFrame]) -> List[Dict]:
        signals = []
        now = datetime.now()
        # Only run after 10:00 AM (45 min after open)
        if now.hour == 9 and now.minute < 15 + self.params["start_after_minutes"]:
            return []

        for ticker, df in data_dict.items():
            try:
                signal = self._compute_signal(ticker, df, now)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug(f"VWAP error {ticker}: {e}")

        return signals[:self.params["max_positions"]]

    def _compute_signal(self, ticker: str, df: pd.DataFrame, now: datetime) -> Optional[Dict]:
        today = now.date()
        today_df = df[df.index.date == today].copy()

        if len(today_df) < self.params["min_trend_bars"] + 2:
            return None

        vwap = self.ti.vwap(today_df)
        supertrend = self.ti.supertrend(today_df, self.params["supertrend_period"], self.params["supertrend_mult"])
        atr = self.ti.atr(today_df).iloc[-1]

        current = today_df.iloc[-1]
        prev = today_df.iloc[-2]
        current_close = current['close']
        current_vwap = vwap.iloc[-1]
        current_st = supertrend.iloc[-1]

        vwap_touch = abs(current_close - current_vwap) / current_vwap <= self.params["vwap_touch_pct"]

        # BUY: Supertrend bullish, price touched VWAP and bouncing up
        if current_st == 1 and vwap_touch and current_close > prev['close']:
            stop = current_vwap - self.params["stop_atr_mult"] * atr
            target = current_close + self.params["target_atr_mult"] * atr
            return {
                "ticker": ticker, "signal": "BUY",
                "price": round(current_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "vwap": round(current_vwap, 2),
                "supertrend": int(current_st),
                "atr": round(atr, 2),
                "strategy": "VWAP_Pullback",
                "time": str(today_df.index[-1]),
            }

        # SELL: Supertrend bearish, price touched VWAP and rejecting
        if current_st == -1 and vwap_touch and current_close < prev['close']:
            stop = current_vwap + self.params["stop_atr_mult"] * atr
            target = current_close - self.params["target_atr_mult"] * atr
            return {
                "ticker": ticker, "signal": "SELL_SHORT",
                "price": round(current_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "vwap": round(current_vwap, 2),
                "supertrend": int(current_st),
                "atr": round(atr, 2),
                "strategy": "VWAP_Pullback",
                "time": str(today_df.index[-1]),
            }

        return None
