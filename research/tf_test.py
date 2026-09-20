#!/usr/bin/env python3
"""Hypothesis: finer timeframe + tighter spread => higher daily harvest rate.
Test harvester on 15m (scaled indicators) vs 1h, reporting ROI/day per window.
Skeptical: tighter-spread touch-fill inflates; we also charge a maker proxy."""
import os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from runner import metrics_from_equity
from backtest import GridBacktester
from strategy import GRID_PARAMS

PORT7 = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
         "LINK/USDT", "LTC/USDT"]
WINDOWS = [90, 180, 365]  # 15m cache ~400d


def bars_per_hour(tf):
    return {"1h": 1, "15m": 4, "5m": 12}[tf]


def harvester_params(tf, spread, levels, maker):
    bph = bars_per_hour(tf)
    p = dict(GRID_PARAMS)
    p.update(dict(
        weight_factor=0.0, max_inv_ratio=0.30, inv_target=0.18,
        spread=spread, levels=levels,
        maker_fee=maker, taker_fee=0.001,
        rsi_period=14 * bph,          # keep 14h RSI
        bb_period=20 * bph,           # keep 20h BB
        rebalance_every=bph,          # hourly rebalance
    ))
    return p


def grid_eq(candles, capital, params):
    bt = GridBacktester(capital=capital, **params)
    eq = np.empty(len(candles))
    for i, r in enumerate(candles.tolist()):
        bt.process_candle(r); eq[i] = bt.capital + bt.btc_held * r[4]
    return eq, bt.total_trades


def sub_for(asset, tf, days):
    arr = dc.load("binance", asset, tf); end = arr[-1, 0]
    return arr[arr[:, 0] >= end - days * 86400 * 1000]


def port_window(assets, tf, days, spread, levels, maker):
    params = harvester_params(tf, spread, levels, maker)
    subs = {a: sub_for(a, tf, days) for a in assets}
    minlen = min(len(subs[a]) for a in assets)
    subs = {a: subs[a][-minlen:] for a in assets}
    eq_total = np.zeros(minlen); trades = 0
    for a in assets:
        eq, tr = grid_eq(subs[a], 1000.0 / len(assets), params)
        eq_total += eq; trades += tr
    m = metrics_from_equity(subs[assets[0]][:, 0], eq_total, 1000.0, days, 1.0, 1.0)
    m["trades"] = trades
    return m


def run(label, tf, spread, levels, maker):
    outs = {}
    for d in WINDOWS:
        outs[d] = port_window(PORT7, tf, d, spread, levels, maker)
    drois = [outs[d]["roi"] / d for d in WINDOWS]
    rois = [outs[d]["roi"] for d in WINDOWS]
    dd = max(outs[d]["max_dd"] for d in WINDOWS)
    allp = all(r > 0 for r in rois)
    print(f"  {label:34s} " +
          " ".join(f"{d}:{outs[d]['roi']:+6.1f}%({outs[d]['roi']/d:+.3f}/j)" for d in WINDOWS) +
          f" | maxDD={dd:4.1f} minDaily={min(drois):+.3f}%/j {'ALL+' if allp else '   '}")


if __name__ == "__main__":
    print("### PORTFOLIO-7 daily-ROI: 1h vs 15m, spread sweep (maker proxy 0.02%)")
    print("# baseline 1h:")
    run("1h spread1.0% lv4", "1h", 0.010, 4, 0.0002)
    print("# 15m tighter spreads:")
    for sp in [0.004, 0.005, 0.006, 0.008]:
        run(f"15m spread{sp*100:.1f}% lv4", "15m", sp, 4, 0.0002)
    print("# 15m with maker=0 (true MEXC limit) — optimistic bound:")
    for sp in [0.004, 0.006]:
        run(f"15m spread{sp*100:.1f}% lv4 mk0", "15m", sp, 4, 0.0)
    print("# 15m more levels:")
    for lv in [5, 6]:
        run(f"15m spread0.5% lv{lv}", "15m", 0.005, lv, 0.0002)
