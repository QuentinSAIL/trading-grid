#!/usr/bin/env python3
"""Fast offline multi-window sweep, ranked by DAILY ROI (target >=0.2%/day).
Tests a range of harvester params across windows and (optionally) a portfolio,
so we see the whole landscape/plateau instead of cherry-picking a point.

Usage: edit GRID below or import eval_config / sweep from other scripts.
"""
import os, sys, itertools
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from runner import metrics_from_equity
from backtest import GridBacktester
from strategy import GRID_PARAMS

PORT7 = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
         "LINK/USDT", "LTC/USDT"]
BPH = {"1h": 1, "15m": 4, "5m": 12}

_CACHE = {}
def sub_for(asset, tf, days):
    key = (asset, tf)
    if key not in _CACHE:
        _CACHE[key] = dc.load("binance", asset, tf)
    arr = _CACHE[key]; end = arr[-1, 0]
    return arr[arr[:, 0] >= end - days * 86400 * 1000]


def make_params(tf, spread, levels, maker, max_inv, inv_target,
                inv_target_max=0.30, weight_factor=0.0, rsi_strength=4.0):
    bph = BPH[tf]
    p = dict(GRID_PARAMS)
    p.update(dict(weight_factor=weight_factor, max_inv_ratio=max_inv,
                  inv_target=inv_target, inv_target_max=inv_target_max,
                  spread=spread, levels=levels,
                  maker_fee=maker, taker_fee=0.001, rsi_strength=rsi_strength,
                  rsi_period=14 * bph, bb_period=20 * bph, rebalance_every=bph))
    return p


def grid_eq(candles, capital, params):
    bt = GridBacktester(capital=capital, **params)
    eq = np.empty(len(candles))
    for i, r in enumerate(candles.tolist()):
        bt.process_candle(r); eq[i] = bt.capital + bt.btc_held * r[4]
    return eq


def eval_config(params, tf, assets, windows):
    out = {}
    for d in windows:
        subs = {a: sub_for(a, tf, d) for a in assets}
        minlen = min(len(subs[a]) for a in assets)
        subs = {a: subs[a][-minlen:] for a in assets}
        eq = np.zeros(minlen)
        for a in assets:
            eq += grid_eq(subs[a], 1000.0 / len(assets), params)
        out[d] = metrics_from_equity(subs[assets[0]][:, 0], eq, 1000.0, d, 1.0, 1.0)
    rois = [out[d]["roi"] for d in windows]
    drois = [out[d]["roi"] / d for d in windows]
    return {
        "windows": out,
        "min_daily": min(drois), "mean_daily": sum(drois) / len(drois),
        "worst_roi": min(rois), "all_positive": all(r > 0 for r in rois),
        "max_dd": max(out[d]["max_dd"] for d in windows),
    }


def sweep(grid, tf, assets, windows, topn=12):
    keys = list(grid.keys()); vals = list(grid.values())
    combos = list(itertools.product(*vals))
    print(f"# {len(combos)} configs x {len(windows)} windows on {tf} "
          f"({len(assets)} assets)")
    results = []
    for combo in combos:
        kw = dict(zip(keys, combo))
        params = make_params(tf, **kw)
        try:
            r = eval_config(params, tf, assets, windows)
            r["cfg"] = kw
            results.append(r)
        except Exception as e:
            print("  err", kw, e)
    results.sort(key=lambda r: (r["min_daily"], r["mean_daily"]), reverse=True)
    print(f"\n{'rank':>4} {'minDaily':>9} {'meanDaily':>9} {'maxDD':>6} {'ALL+':>5}  config")
    for i, r in enumerate(results[:topn]):
        cfg = " ".join(f"{k}={v}" for k, v in r["cfg"].items())
        wins = " ".join(f"{d}:{r['windows'][d]['roi']/d:+.3f}" for d in windows)
        print(f"{i+1:>4} {r['min_daily']:>+8.3f}% {r['mean_daily']:>+8.3f}% "
              f"{r['max_dd']:>5.1f}% {str(r['all_positive']):>5}  {cfg}")
        print(f"       daily/win: {wins}")
    return results


if __name__ == "__main__":
    tf = sys.argv[1] if len(sys.argv) > 1 else "1h"
    windows = [90, 180, 365] if tf != "1h" else [90, 180, 365, 800]
    GRID = {
        "spread":         [0.010, 0.013],
        "levels":         [4],
        "max_inv":        [0.90],          # ne bloque pas; inv_target_max pilote
        "inv_target":     [0.18],
        "inv_target_max": [0.30, 0.45, 0.60, 0.80],
        "maker":          [0.0002],
    }
    sweep(GRID, tf, PORT7, windows, topn=15)
