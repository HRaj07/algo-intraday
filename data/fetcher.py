"""
Intraday Data Fetcher
Fetches 15-minute OHLCV data using yfinance (free)
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)


class IntradayFetcher:
    """Fetches 15-minute intraday data from Yahoo Finance."""

    def fetch_intraday(self, tickers: List[str], days_back: int = 5) -> Dict[str, pd.DataFrame]:
        """Fetch 15-minute OHLCV data for multiple tickers."""
        results = {}
        for ticker in tickers:
            try:
                df = yf.download(
                    ticker,
                    period=f"{days_back}d",
                    interval="15m",
                    auto_adjust=True,
                    progress=False,
                    multi_level_index=False,
                )
                if df.empty:
                    continue
                df.columns = [c.lower() for c in df.columns]
                # Remove timezone info
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                results[ticker] = df.dropna()
                time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Failed {ticker}: {e}")
        logger.info(f"Fetched 15min data for {len(results)}/{len(tickers)} tickers")
        return results

    def get_today_data(self, tickers: List[str]) -> Dict[str, pd.DataFrame]:
        """Get only today's 15-min bars."""
        all_data = self.fetch_intraday(tickers, days_back=3)
        today = datetime.now().date()
        today_data = {}
        for ticker, df in all_data.items():
            today_df = df[df.index.date == today]
            if not today_df.empty:
                today_data[ticker] = today_df
        return today_data

    def get_historical_15m(self, tickers: List[str], days: int = 60) -> Dict[str, pd.DataFrame]:
        """Get up to 60 days of 15-min data (yfinance free limit)."""
        return self.fetch_intraday(tickers, days_back=min(days, 60))


class TechnicalIndicators:
    """Compute intraday technical indicators."""

    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        """Volume Weighted Average Price - resets each day."""
        df = df.copy()
        df['date'] = df.index.date
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3
        df['tp_vol'] = df['tp'] * df['volume']
        df['cum_tp_vol'] = df.groupby('date')['tp_vol'].cumsum()
        df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        return df['cum_tp_vol'] / df['cum_vol']

    @staticmethod
    def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
        close = df['close']
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        return 100 - (100 / (1 + rs)).fillna(50)

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, prev_close = df['high'], df['low'], df['close'].shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, adjust=False).mean()

    @staticmethod
    def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
        """Returns +1 (bullish) or -1 (bearish)"""
        high, low, close = df['high'], df['low'], df['close']
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = tr.ewm(com=period - 1, adjust=False).mean()
        hl2 = (high + low) / 2
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr
        direction = pd.Series(1, index=close.index)
        direction[close < lower] = -1
        direction[close > upper] = 1
        return direction.replace(0, np.nan).ffill().fillna(1).astype(int)

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, close = df['high'], df['low'], df['close']
        prev_close = close.shift(1)
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        plus_dm[high.diff() <= -low.diff()] = 0
        minus_dm[-low.diff() <= high.diff()] = 0
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = tr.ewm(com=period - 1, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(com=period - 1, adjust=False).mean().fillna(0)
