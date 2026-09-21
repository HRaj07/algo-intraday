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

    # Yahoo accepts a list of symbols in one call. At 214 names the old
    # one-request-per-ticker loop meant 216 sequential HTTP calls every 15
    # minutes - roughly 6,900 a day - and Yahoo throttled it. PIDILITE and LTIM
    # came back "possibly delisted" when they are nothing of the sort.
    #
    # Worse, the failure was SILENT: the bot carried on with whatever subset
    # survived, and nothing recorded which names went missing. A universe that
    # quietly shrinks is a universe you cannot reason about.
    BATCH_SIZE = 40

    def fetch_intraday(self, tickers: List[str], days_back: int = 5) -> Dict[str, pd.DataFrame]:
        """Fetch 15-minute OHLCV for many tickers, batched to avoid throttling."""
        import yfinance as yf

        results: Dict[str, pd.DataFrame] = {}
        missing: List[str] = []

        for i in range(0, len(tickers), self.BATCH_SIZE):
            batch = tickers[i:i + self.BATCH_SIZE]
            try:
                raw = yf.download(
                    batch, period=f"{days_back}d", interval="15m",
                    auto_adjust=True, progress=False, group_by="ticker",
                    threads=True,
                )
            except Exception as e:
                logger.warning(f"batch {i // self.BATCH_SIZE} failed: {e}")
                missing.extend(batch)
                continue

            if raw is None or raw.empty:
                missing.extend(batch)
                continue

            for t in batch:
                try:
                    if isinstance(raw.columns, pd.MultiIndex):
                        if t not in raw.columns.get_level_values(0):
                            missing.append(t)
                            continue
                        df = raw[t].copy()
                    else:
                        df = raw.copy()          # single-symbol batch
                    df.columns = [str(c).lower() for c in df.columns]
                    df = df.dropna(how="all")
                    if df.empty or "close" not in df.columns:
                        missing.append(t)
                        continue
                    results[t] = self._to_ist(df).dropna()
                except Exception as e:
                    logger.debug(f"{t}: {e}")
                    missing.append(t)

            time.sleep(0.5)   # gentle between batches, not between tickers

        logger.info(f"Fetched 15min data for {len(results)}/{len(tickers)} tickers")
        if missing:
            # Name them. A silently shrinking universe is the failure mode here.
            head = ", ".join(missing[:12])
            more = f" (+{len(missing) - 12} more)" if len(missing) > 12 else ""
            logger.warning(f"NO DATA for {len(missing)} symbol(s): {head}{more}")
        return results

    @staticmethod
    def _to_ist(df: pd.DataFrame) -> pd.DataFrame:
        """Convert the index to IST and strip tz, so .date gives IST dates."""
        from tzutil import IST as _ist
        if df.index.tz is not None:
            df.index = df.index.tz_convert(_ist).tz_localize(None)
        else:
            df.index = df.index.tz_localize("UTC").tz_convert(_ist).tz_localize(None)
        return df

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
