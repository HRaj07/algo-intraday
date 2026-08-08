"""
Opening Range Breakout (ORB) Strategy
Best documented intraday strategy for NSE - AlgoTest.in
Win Rate: 45-55% | Profit Factor: 1.4-2.0 | R:R = 1:2
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from data.fetcher import TechnicalIndicators
from config import ORB

logger = logging.getLogger(__name__)


class ORBStrategy:
    def __init__(self):
        self.params = ORB
        self.ti = TechnicalIndicators()
        self.name = "ORB"

    def compute_signals(self, data_dict: Dict[str, pd.DataFrame]) -> List[Dict]:
        """Compute ORB signals across all tickers."""
        signals = []
        now = datetime.now()

        for ticker, df in data_dict.items():
            if len(df) < 2:
                continue
            try:
                signal = self._compute_signal(ticker, df, now)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug(f"ORB error {ticker}: {e}")

        return signals[:self.params["max_positions"]]

    def _compute_signal(self, ticker: str, df: pd.DataFrame, now: datetime) -> Optional[Dict]:
        """Check if ORB breakout occurred on current bar."""
        today = now.date()
        today_df = df[df.index.date == today].copy()

        if len(today_df) < 2:
            return None

        # Opening range = first bar (9:15-9:30)
        orb_bar = today_df.iloc[0]
        orb_high = orb_bar['high']
        orb_low = orb_bar['low']
        orb_range = orb_high - orb_low

        # Skip if range too wide or too tight
        orb_range_pct = orb_range / orb_bar['close']
        if orb_range_pct > self.params["max_range_pct"]:
            return None
        if orb_range_pct < self.params["min_range_pct"]:
            return None

        # Current bar (latest)
        current_bar = today_df.iloc[-1]
        current_close = current_bar['close']
        current_time = today_df.index[-1]

        # Must be after opening range bar
        if len(today_df) < 2:
            return None

        # VWAP filter
        vwap = self.ti.vwap(today_df).iloc[-1]

        # Volume confirmation: current bar volume vs average of first 5 bars
        avg_open_vol = today_df['volume'].iloc[:5].mean()
        current_vol = current_bar['volume']
        vol_ok = current_vol >= avg_open_vol * self.params["volume_multiplier"]

        # Breakout signals
        prev_close = today_df['close'].iloc[-2]

        # BUY: breakout above ORB high
        if current_close > orb_high and prev_close <= orb_high:
            if self.params["vwap_filter"] and current_close < vwap:
                return None  # Don't buy below VWAP
            stop = orb_low
            target = current_close + self.params["risk_reward"] * (current_close - stop)
            return {
                "ticker": ticker, "signal": "BUY",
                "price": round(current_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "orb_high": round(orb_high, 2),
                "orb_low": round(orb_low, 2),
                "vwap": round(vwap, 2),
                "volume_ratio": round(current_vol / avg_open_vol if avg_open_vol > 0 else 1, 2),
                "strategy": "ORB",
                "time": str(current_time),
            }

        # SELL: breakdown below ORB low
        if current_close < orb_low and prev_close >= orb_low:
            if self.params["vwap_filter"] and current_close > vwap:
                return None  # Don't short above VWAP
            stop = orb_high
            target = current_close - self.params["risk_reward"] * (stop - current_close)
            return {
                "ticker": ticker, "signal": "SELL_SHORT",
                "price": round(current_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "orb_high": round(orb_high, 2),
                "orb_low": round(orb_low, 2),
                "vwap": round(vwap, 2),
                "volume_ratio": round(current_vol / avg_open_vol if avg_open_vol > 0 else 1, 2),
                "strategy": "ORB",
                "time": str(current_time),
            }

        return None
