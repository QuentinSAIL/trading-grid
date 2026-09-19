#!/usr/bin/env python3
"""Download & cache OHLCV candles to local .npy for fast offline backtesting.

Cache format: research/data/<exchange>_<symbol>_<timeframe>.npy
Columns: [timestamp_ms, open, high, low, close, volume]  (float64)
"""
import os
import sys
import time
import numpy as np
import ccxt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _cache_path(exchange_id, symbol, timeframe):
    safe = symbol.replace("/", "-")
    return os.path.join(DATA_DIR, f"{exchange_id}_{safe}_{timeframe}.npy")


def download(exchange_id, symbol, timeframe, days, force=False):
    path = _cache_path(exchange_id, symbol, timeframe)
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": 30000})
    ex.load_markets()
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    now = ex.milliseconds()
    since = now - days * 86400 * 1000
    all_c = []
    cur = since
    while cur < now:
        for attempt in range(5):
            try:
                batch = ex.fetch_ohlcv(symbol, timeframe, cur, 1000)
                break
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                print(f"  retry {attempt}: {e}", file=sys.stderr)
                time.sleep(2)
        else:
            break
        if not batch:
            break
        all_c.extend(batch)
        cur = batch[-1][0] + tf_ms
        if batch[-1][0] >= now:
            break
        print(f"\r  {exchange_id} {symbol} {timeframe}: {len(all_c)} candles...",
              end="", file=sys.stderr)
    print(file=sys.stderr)
    # dedup + sort by timestamp
    seen = {}
    for c in all_c:
        seen[int(c[0])] = c
    rows = [seen[k] for k in sorted(seen)]
    arr = np.array(rows, dtype=np.float64)
    np.save(path, arr)
    print(f"saved {len(arr)} candles -> {path}")
    return arr


def load(exchange_id, symbol, timeframe):
    path = _cache_path(exchange_id, symbol, timeframe)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No cache: {path}. Run download first.")
    return np.load(path)


def load_days(exchange_id, symbol, timeframe, days):
    """Return the last `days` days of candles as list-of-lists (backtest format)."""
    arr = load(exchange_id, symbol, timeframe)
    if len(arr) == 0:
        return []
    end = arr[-1, 0]
    cutoff = end - days * 86400 * 1000
    sub = arr[arr[:, 0] >= cutoff]
    return sub


if __name__ == "__main__":
    # Default: download everything we need for research
    jobs = [
        ("mexc", "BTC/USDT", "1h", 900),
        ("mexc", "BTC/USDT", "15m", 400),
        ("binance", "BTC/USDT", "1h", 900),
        ("binance", "ETH/USDT", "1h", 900),
        ("binance", "SOL/USDT", "1h", 900),
        ("binance", "BTC/USDT", "4h", 900),
    ]
    if len(sys.argv) > 1:
        # custom: exchange symbol timeframe days
        e, s, t, d = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
        download(e, s, t, d)
    else:
        for e, s, t, d in jobs:
            try:
                download(e, s, t, d)
            except Exception as ex:
                print(f"FAIL {e} {s} {t}: {ex}", file=sys.stderr)
