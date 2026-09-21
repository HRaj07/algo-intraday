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

# yfinance is imported lazily inside fetch_intraday(). Importing it at module
# scope means TechnicalIndicators cannot be used - by a test harness, a
# backtester, or an offline analysis - without the network library present.
logger = logging.getLogger(__name__)


class IntradayFetcher:
    """Fetches 15-minute intraday data from Yahoo Finance."""

    def fetch_intraday(self, tickers: List[str], days_back: int = 5) -> Dict[str, pd.DataFrame]:
        """Fetch 15-minute OHLCV data for multiple tickers."""
        import yfinance as yf

        results = {}
        for ticker in tickers:
            try:
                # multi_level_index only exists in newer yfinance; guard for older versions
                try:
                    df = yf.download(
                        ticker,
                        period=f"{days_back}d",
                        interval="15m",
                        auto_adjust=True,
                        progress=False,
                        multi_level_index=False,
                    )
                except TypeError:
                    df = yf.download(
                        ticker,
                        period=f"{days_back}d",
                        interval="15m",
                        auto_adjust=True,
                        progress=False,
                    )
                if df.empty:
                    continue
                # Flatten multi-level columns if present (older yfinance returns MultiIndex)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0].lower() for c in df.columns]
                else:
                    df.columns = [c.lower() for c in df.columns]
                # Convert to IST then strip tz so df.index.date returns IST dates
                from tzutil import IST as _ist
                if df.index.tz is not None:
                    df.index = df.index.tz_convert(_ist).tz_localize(None)
                else:
                    # Assume UTC if tz-naive (yfinance sometimes returns naive UTC)
                    df.index = df.index.tz_localize("UTC").tz_convert(_ist).tz_localize(None)
                results[ticker] = df.dropna()
                time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Failed {ticker}: {e}")
        logger.info(f"Fetched 15min data for {len(results)}/{len(tickers)} tickers")
        return results

    def get_today_data(self, tickers: List[str]) -> Dict[str, pd.DataFrame]:
        """Get only today's 15-min bars."""
        from tzutil import now_ist
        all_data = self.fetch_intraday(tickers, days_back=3)
        today = now_ist().date()
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
        """
        Volume Weighted Average Price, reset each day.

        INDICES HAVE NO VOLUME. Yahoo returns volume=0 for ^NSEI, so the naive
        cum(tp*vol)/cum(vol) divides by zero and returns NaN for every bar. That
        NaN then silently disabled the regime gate: the check is
        `if deviation > tolerance: stand down`, and `nan > 0.003` is False, so
        the gate waved everything through instead of standing down. The one
        safety feature meant to stop dip-buying into a falling market was a
        no-op, and nothing in the logs said so.

        Where cumulative volume is zero, fall back to the expanding mean of the
        typical price - the unweighted equivalent, which is the right measure for
        an index and matches VWAP exactly when volume is flat.
        """
        df = df.copy()
        df['date'] = df.index.date
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3
        df['tp_vol'] = df['tp'] * df['volume']
        cum_tp_vol = df.groupby('date')['tp_vol'].cumsum()
        cum_vol = df.groupby('date')['volume'].cumsum()

        vwap = cum_tp_vol / cum_vol.replace(0, np.nan)

        # Unweighted fallback for zero-volume series (indices).
        unweighted = df.groupby('date')['tp'].expanding().mean().reset_index(
            level=0, drop=True)
        return vwap.fillna(unweighted)

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
