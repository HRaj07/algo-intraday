"""
Data health check. Replaces the old test_yf.py / test_yf2.py / test_yf3.py probes.

Run this when the bot is quiet and you want to know whether that is the market
being calm or the data feed being broken:

    python check_data.py

The distinction matters because v2's regime gate FAILS CLOSED: no NIFTY data
means no trades, by design. A broken ^NSEI fetch and a genuinely unattractive
market produce identical logs, so check rather than guess.
"""
import sys

from config import INTRADAY_UNIVERSE, INDEX_TICKER, VIX_TICKER
from data.fetcher import IntradayFetcher, TechnicalIndicators
from tzutil import now_ist

ti = TechnicalIndicators()


def main():
    now = now_ist()
    print(f"Data check | {now:%Y-%m-%d %H:%M IST}\n")

    fetcher = IntradayFetcher()
    symbols = INTRADAY_UNIVERSE + [INDEX_TICKER, VIX_TICKER]
    data = fetcher.fetch_intraday(symbols, days_back=20)

    missing = [s for s in symbols if s not in data]
    print(f"Fetched {len(data)}/{len(symbols)} symbols")
    if missing:
        print(f"  MISSING: {', '.join(missing)}")

    print()
    problems = []

    # --- the one that decides whether the bot can trade at all ---
    idx = data.get(INDEX_TICKER)
    if idx is None or idx.empty:
        problems.append(
            f"{INDEX_TICKER} returned no data. The regime gate fails closed, so the "
            f"bot will not trade at all until this is fixed. This is the single "
            f"most important symbol in the system."
        )
    else:
        today = idx[idx.index.date == now.date()]
        print(f"{INDEX_TICKER}: {len(idx)} bars total, {len(today)} today")
        if len(today) >= 2:
            import math
            vwap = ti.vwap(today).iloc[-1]
            close = today["close"].iloc[-1]
            if not (math.isfinite(vwap) and math.isfinite(close)):
                problems.append(
                    f"{INDEX_TICKER} VWAP is not finite ({vwap}). The regime gate "
                    f"cannot evaluate and will stand down. Indices report zero "
                    f"volume; the unweighted fallback in TechnicalIndicators.vwap "
                    f"should prevent this - if you see it, that fallback is broken."
                )
            else:
                dev = (vwap - close) / vwap * 100
                print(f"  last {close:,.2f} | VWAP {vwap:,.2f} | {dev:+.2f}% vs VWAP")
                print(f"  regime gate would be: "
                      f"{'OPEN' if dev <= 0.3 else 'CLOSED (index below its VWAP)'}")
        elif now.hour >= 10:
            problems.append(f"{INDEX_TICKER} has only {len(today)} bars today")

    # --- VIX is optional: absent means the VIX check is skipped, not a failure ---
    vix = data.get(VIX_TICKER)
    if vix is None or vix.empty:
        print(f"\n{VIX_TICKER}: no data (the VIX filter is skipped, not fatal)")
    else:
        print(f"\n{VIX_TICKER}: last {vix['close'].iloc[-1]:.2f}")

    # --- universe coverage ---
    print()
    thin = []
    for t in INTRADAY_UNIVERSE:
        df = data.get(t)
        if df is None or df.empty:
            thin.append(f"{t} (no data)")
            continue
        today = df[df.index.date == now.date()]
        if len(today) < 4 and now.hour >= 10:
            thin.append(f"{t} ({len(today)} bars today)")
    print(f"Universe: {len(INTRADAY_UNIVERSE) - len(thin)}/{len(INTRADAY_UNIVERSE)} usable")
    if thin:
        print(f"  thin or missing: {', '.join(thin)}")
    if len(thin) > len(INTRADAY_UNIVERSE) // 3:
        problems.append("more than a third of the universe is unusable")

    print()
    if problems:
        print("PROBLEMS FOUND:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("Data looks healthy. A quiet bot is the filters working, not a feed fault.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
