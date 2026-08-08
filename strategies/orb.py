"""
30-Minute Institutional Opening Range Breakout (ORB-30) Strategy
- 30-min opening range (9:15-9:45 AM)
- Multi-factor filtering: ADX trend strength, VWAP bias, Volatility limits
- Asymmetric 1:2.0 Risk-to-Reward with automated Breakeven Trailing Stop
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from data.fetcher import TechnicalIndicators
from config import ORB_CONFIG, SYSTEM

logger = logging.getLogger(__name__)


class ORB30Strategy:
    def __init__(self):
        self.params = ORB_CONFIG
        self.ti = TechnicalIndicators()
        self.name = "ORB-30"

    def compute_signals(self, data_dict: Dict[str, pd.DataFrame]) -> List[Dict]:
        """Scan all tickers for high-conviction 30m ORB breakouts."""
        signals = []
        now = datetime.now()

        for ticker, df in data_dict.items():
            if len(df) < 4:
                continue
            try:
                sig = self._compute_signal(ticker, df, now)
                if sig:
                    signals.append(sig)
            except Exception as e:
                logger.debug(f"ORB error on {ticker}: {e}")

        # Rank signals by ADX score (strongest trend first)
        ranked = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)
        return ranked[:SYSTEM["max_daily_trades"]]

    def _compute_signal(self, ticker: str, df: pd.DataFrame, now: datetime) -> Optional[Dict]:
        today = now.date()
        today_df = df[df.index.date == today].copy()

        # Need at least 3 bars (Bar 0: 9:15-9:30, Bar 1: 9:30-9:45, Bar 2+: Breakout checks)
        if len(today_df) < 3:
            return None

        # Build 30-min Opening Range
        bar0, bar1 = today_df.iloc[0], today_df.iloc[1]
        r30_hi = max(bar0['high'], bar1['high'])
        r30_lo = min(bar0['low'], bar1['low'])
        rng = r30_hi - r30_lo
        c_open = bar1['close']

        if c_open <= 0:
            return None

        rng_pct = rng / c_open
        if rng_pct > self.params["max_range_pct"] or rng_pct < self.params["min_range_pct"]:
            return None

        # Technical indicators
        adx_series = self.ti.adx(df)
        adx_val = adx_series.iloc[-1] if not adx_series.empty else 0
        if adx_val < self.params["min_adx"]:
            return None

        vwap_series = self.ti.vwap(today_df)
        curr_bar = today_df.iloc[-1]
        prev_bar = today_df.iloc[-2]
        curr_close = curr_bar['close']
        prev_close = prev_bar['close']
        curr_vwap = vwap_series.iloc[-1]
        bar_idx = len(today_df) - 1

        # Breakout must occur within morning institutional window (before 11:15 AM = bar index <= 8)
        if bar_idx > self.params["scan_window_end_bar"]:
            return None

        # LONG Breakout: Crosses above 30m high, above VWAP
        if curr_close > r30_hi and prev_close <= r30_hi and curr_close > curr_vwap:
            stop = r30_lo
            risk = curr_close - stop
            if risk <= 0:
                return None
            target = curr_close + self.params["risk_reward"] * risk
            return {
                "ticker": ticker,
                "signal": "BUY",
                "price": round(curr_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "range_high": round(r30_hi, 2),
                "range_low": round(r30_lo, 2),
                "vwap": round(curr_vwap, 2),
                "adx": round(adx_val, 1),
                "score": round(adx_val, 2),
                "risk": round(risk, 2),
                "strategy": "ORB-30",
                "time": str(today_df.index[-1]),
            }

        # SHORT Breakdown: Crosses below 30m low, below VWAP
        if curr_close < r30_lo and prev_close >= r30_lo and curr_close < curr_vwap:
            stop = r30_hi
            risk = stop - curr_close
            if risk <= 0:
                return None
            target = curr_close - self.params["risk_reward"] * risk
            return {
                "ticker": ticker,
                "signal": "SELL_SHORT",
                "price": round(curr_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "range_high": round(r30_hi, 2),
                "range_low": round(r30_lo, 2),
                "vwap": round(curr_vwap, 2),
                "adx": round(adx_val, 1),
                "score": round(adx_val, 2),
                "risk": round(risk, 2),
                "strategy": "ORB-30",
                "time": str(today_df.index[-1]),
            }

        return None
