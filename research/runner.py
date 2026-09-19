#!/usr/bin/env python3
"""Fast offline benchmark harness for trading strategies.

Loads cached candles, runs a strategy across multiple windows/assets,
computes rich metrics (ROI, Sharpe, Sortino, maxDD, Calmar, vs B&H).
"""
import os
import sys
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # repo root for backtest.py
import data_cache as dc

WINDOWS = [90, 180, 365, 800]
HOURS_PER_YEAR = 24 * 365


def metrics_from_equity(ts, equity, initial, days, start_price, end_price):
    equity = np.asarray(equity, dtype=np.float64)
    if len(equity) < 2:
        return {}
    final = equity[-1]
    pnl = final - initial
    roi = pnl / initial * 100
    daily_roi = roi / days if days else 0
    # drawdown
    peak = np.maximum.accumulate(equity)
    dd_series = (peak - equity) / peak
    max_dd = dd_series.max() * 100
    # hourly returns (equity sampled per candle ~ hourly)
    rets = np.diff(equity) / equity[:-1]
    rets = rets[np.isfinite(rets)]
    if len(rets) > 1 and rets.std() > 0:
        sharpe = rets.mean() / rets.std() * math.sqrt(HOURS_PER_YEAR)
        downside = rets[rets < 0]
        sortino = (rets.mean() / downside.std() * math.sqrt(HOURS_PER_YEAR)
                   if len(downside) > 1 and downside.std() > 0 else 0.0)
    else:
        sharpe = sortino = 0.0
    # annualized return for Calmar
    years = days / 365 if days else 1
    ann_ret = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else -100
    calmar = ann_ret / max_dd if max_dd > 0 else 0.0
    bh = (end_price - start_price) / start_price * 100
    return {
        "roi": roi, "daily_roi": daily_roi, "final": final,
        "max_dd": max_dd, "sharpe": sharpe, "sortino": sortino,
        "calmar": calmar, "ann_ret": ann_ret, "bh": bh,
        "vs_hold": roi - bh, "pnl": pnl,
    }


def run_strategy(strategy_factory, candles, initial=1000.0):
    """strategy_factory() -> object with .process_candle(row)->None and
       .equity(price)->float and attributes total_trades.
    Returns (metrics_inputs)."""
    strat = strategy_factory()
    ts_list = []
    eq_list = []
    for row in candles:
        strat.process_candle(row)
        ts_list.append(row[0])
        eq_list.append(strat.equity(row[4]))
    return strat, ts_list, eq_list


def bench(strategy_factory, exchange="binance", symbol="BTC/USDT",
          timeframe="1h", windows=WINDOWS, initial=1000.0, label=""):
    arr = dc.load(exchange, symbol, timeframe)
    end = arr[-1, 0]
    out = {}
    for days in windows:
        cutoff = end - days * 86400 * 1000
        sub = arr[arr[:, 0] >= cutoff]
        candles = sub.tolist()
        strat, ts, eq = run_strategy(strategy_factory, candles, initial)
        m = metrics_from_equity(ts, eq, initial, days,
                                sub[0, 1], sub[-1, 4])
        m["trades"] = getattr(strat, "total_trades", 0)
        m["days"] = days
        out[days] = m
    return out


def fmt_row(days, m):
    return (f"  {days:4d}j: ROI={m['roi']:+8.2f}%  "
            f"{m['daily_roi']:+.3f}%/j  maxDD={m['max_dd']:5.1f}%  "
            f"Sharpe={m['sharpe']:+5.2f}  Sortino={m['sortino']:+6.2f}  "
            f"Calmar={m['calmar']:+6.2f}  B&H={m['bh']:+7.1f}%  "
            f"vsHold={m['vs_hold']:+7.1f}%  trades={m['trades']}")


def print_bench(name, results):
    print(f"\n=== {name} ===")
    for days in sorted(results):
        print(fmt_row(days, results[days]))
    # aggregate score: min ROI across windows (worst case), and mean
    rois = [results[d]["roi"] for d in results]
    print(f"  >>> worst_ROI={min(rois):+.2f}%  mean_ROI={sum(rois)/len(rois):+.2f}%  "
          f"all_positive={all(r > 0 for r in rois)}")
    return {"worst_roi": min(rois), "mean_roi": sum(rois) / len(rois),
            "all_positive": all(r > 0 for r in rois)}


# ---- Adapter for the existing GridBacktester ----
def gridbt_factory(**params):
    from backtest import GridBacktester

    def factory():
        bt = GridBacktester(**params)

        def equity(price):
            return bt.capital + bt.btc_held * price
        bt.equity = equity
        return bt
    return factory


if __name__ == "__main__":
    # Benchmark the current .env.example config
    params = dict(
        capital=1000.0, levels=3, spread=0.008, range_pct=0.04,
        stop_loss_pct=0.25, maker_fee=0.0, taker_fee=0.001,
        grid_type="geometric", weight_factor=-0.9,
        rsi_period=14, rsi_strength=4.0, ema_fast=12, ema_slow=26,
        ema_strength=0.0, bb_period=20, bb_mult=2.0, bb_spread_adapt=True,
        stale_hours=72, decay_per_hour=0.0008, trend_spread_mult=0.0,
        dd_threshold=1.0, dd_factor=0.5, max_inv_ratio=0.20,
        initial_btc_pct=0.25, rebalance_every=1, grid_refresh=0,
        inv_target=0.18, inv_tolerance=0.07,
    )
    res = bench(gridbt_factory(**params))
    print_bench("CURRENT CONFIG (.env.example) — Binance BTC/USDT 1h", res)
