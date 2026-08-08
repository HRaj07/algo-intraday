"""
EMA 9/21 Crossover with Volume & Trend Filter
Win Rate: ~50% | R:R: 1:1.5 | Source: Reddit r/IndiaInvestments, TradingView NSE
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd
from data.fetcher import TechnicalIndicators
from config import EMA_CROSS

logger = logging.getLogger(__name__)


class EMACrossStrategy:
    def __init__(self):
        self.params = EMA_CROSS
        self.ti = TechnicalIndicators()
        self.name = "EMA_Cross"

    def compute_signals(self, data_dict: Dict[str, pd.DataFrame]) -> List[Dict]:
        signals = []
        now = datetime.now()
        if now.hour == 9 and now.minute < 15 + self.params["start_after_minutes"]:
            return []

        for ticker, df in data_dict.items():
            try:
                signal = self._compute_signal(ticker, df, now)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug(f"EMA error {ticker}: {e}")

        return signals[:self.params["max_positions"]]

    def _compute_signal(self, ticker: str, df: pd.DataFrame, now: datetime) -> Optional[Dict]:
        today = now.date()
        today_df = df[df.index.date == today].copy()

        if len(today_df) < 25:
            return None

        close = today_df['close']
        volume = today_df['volume']

        fast_ema = self.ti.ema(close, self.params["fast_ema"])
        slow_ema = self.ti.ema(close, self.params["slow_ema"])
        # Use all available data for trend EMA (need more bars)
        all_close = df['close']
        trend_ema = self.ti.ema(all_close, self.params["trend_ema"])

        current_close = close.iloc[-1]
        current_trend_ema = trend_ema.iloc[-1]
        avg_vol = volume.rolling(10).mean().iloc[-1]
        current_vol = volume.iloc[-1]
        vol_ok = current_vol >= avg_vol * self.params["volume_multiplier"]

        # Crossover detection
        fast_now, fast_prev = fast_ema.iloc[-1], fast_ema.iloc[-2]
        slow_now, slow_prev = slow_ema.iloc[-1], slow_ema.iloc[-2]

        bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
        bearish_cross = fast_prev >= slow_prev and fast_now < slow_now

        # BUY: bullish cross above trend EMA
        if bullish_cross and vol_ok and current_close > current_trend_ema:
            stop = current_close * (1 - self.params["stop_pct"])
            target = current_close * (1 + self.params["target_pct"])
            return {
                "ticker": ticker, "signal": "BUY",
                "price": round(current_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "fast_ema": round(fast_now, 2),
                "slow_ema": round(slow_now, 2),
                "strategy": "EMA_Cross",
                "time": str(today_df.index[-1]),
            }

        # SELL: bearish cross below trend EMA
        if bearish_cross and vol_ok and current_close < current_trend_ema:
            stop = current_close * (1 + self.params["stop_pct"])
            target = current_close * (1 - self.params["target_pct"])
            return {
                "ticker": ticker, "signal": "SELL_SHORT",
                "price": round(current_close, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "fast_ema": round(fast_now, 2),
                "slow_ema": round(slow_now, 2),
                "strategy": "EMA_Cross",
                "time": str(today_df.index[-1]),
            }

        return None
