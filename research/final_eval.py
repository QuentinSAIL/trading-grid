#!/usr/bin/env python3
"""Robustness-first evaluation: non-overlapping OOS blocks + realistic costs.
Tests the wf=0.0 harvester grid + variable trend overlay weight.
"""
import os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from runner import metrics_from_equity
from backtest import GridBacktester
from strategy import GRID_PARAMS, chandelier_alloc, CHAND
from engine import simulate_allocation

WINDOWS = [90, 180, 365, 800]

# The robust harvester found by validation: equal sizing (wf=0.0)
HARVESTER = dict(GRID_PARAMS)
HARVESTER.update(dict(weight_factor=0.0, max_inv_ratio=0.20, inv_target=0.18))


def grid_eq(candles, capital, maker_fee, taker_fee):
    p = dict(HARVESTER); p["maker_fee"] = maker_fee; p["taker_fee"] = taker_fee
    bt = GridBacktester(capital=capital, **p)
    eq = np.empty(len(candles))
    for i, r in enumerate(candles.tolist()):
        bt.process_candle(r)
        eq[i] = bt.capital + bt.btc_held * r[4]
    return eq


def trend_eq(candles, capital, taker_fee, slippage, atr_mult=2.5):
    raw = chandelier_alloc(candles, entry_lb=CHAND["entry_lb"],
                           atr_mult=atr_mult, atr_lb=CHAND["atr_lb"])
    sh = np.roll(raw, 1); sh[0] = 0.0
    eq, _ = simulate_allocation(candles, sh, taker_fee=taker_fee + slippage,
                               band=0.03, initial=capital)
    return np.array(eq)


def blend_eq(candles, capital, wGrid, costs, atr_mult=2.5):
    gk, gt, tt, ts = costs  # grid_maker, grid_taker, trend_taker, trend_slip
    eqA = grid_eq(candles, capital * wGrid, gk, gt)
    if wGrid >= 0.999:
        return eqA
    eqB = trend_eq(candles, capital * (1 - wGrid), tt, ts, atr_mult)
    return eqA + eqB


# cost scenarios: (grid_maker, grid_taker, trend_taker, trend_slip)
COSTS = {
    "mexc-opt":   (0.0,    0.001, 0.001, 0.0003),
    "realistic":  (0.0002, 0.001, 0.001, 0.0005),
    "pessimist":  (0.0005, 0.0015, 0.0015, 0.0010),
}


def eval_trailing(symbol, wGrid, costs, exchange="binance"):
    arr = dc.load(exchange, symbol, "1h"); end = arr[-1, 0]
    out = {}
    for d in WINDOWS:
        sub = arr[arr[:, 0] >= end - d * 86400 * 1000]
        eq = blend_eq(sub, 1000.0, wGrid, costs)
        out[d] = metrics_from_equity(sub[:, 0], eq, 1000.0, d, sub[0, 1], sub[-1, 4])
    return out


def eval_blocks(symbol, wGrid, costs, block_days=150, exchange="binance"):
    arr = dc.load(exchange, symbol, "1h")
    blk_ms = block_days * 86400 * 1000
    start = arr[0, 0]; end = arr[-1, 0]
    blocks = []
    t = start
    while t + blk_ms <= end + 1:
        sub = arr[(arr[:, 0] >= t) & (arr[:, 0] < t + blk_ms)]
        if len(sub) > block_days * 20:
            eq = blend_eq(sub, 1000.0, wGrid, costs)
            m = metrics_from_equity(sub[:, 0], eq, 1000.0, block_days,
                                    sub[0, 1], sub[-1, 4])
            blocks.append(m)
        t += blk_ms
    return blocks


if __name__ == "__main__":
    print("### NON-OVERLAPPING 150d OOS BLOCKS + TRAILING WINDOWS (binance BTC 1h)\n")
    for cost_name in ["mexc-opt", "realistic", "pessimist"]:
        costs = COSTS[cost_name]
        print(f"\n{'='*78}\n### COST = {cost_name}  (gridMaker={costs[0]} gridTaker={costs[1]} trendTaker={costs[2]} slip={costs[3]})\n{'='*78}")
        for wGrid in [1.0, 0.7, 0.6, 0.5, 0.4]:
            blocks = eval_blocks("BTC/USDT", wGrid, costs)
            brois = [b["roi"] for b in blocks]
            npos = sum(1 for r in brois if r > 0)
            trail = eval_trailing("BTC/USDT", wGrid, costs)
            trois = [trail[d]["roi"] for d in WINDOWS]
            tdd = max(trail[d]["max_dd"] for d in WINDOWS)
            allp_trail = all(r > 0 for r in trois)
            print(f"\n  wGrid={wGrid} (trend={1-wGrid:.1f}):")
            print(f"    TRAILING: " + " ".join(f"{d}:{trail[d]['roi']:+6.1f}%" for d in WINDOWS)
                  + f" | worst={min(trois):+.1f} mean={sum(trois)/4:+.1f} maxDD={tdd:.1f} {'ALL+' if allp_trail else ''}")
            print(f"    BLOCKS({len(blocks)}): " + " ".join(f"{r:+6.1f}%" for r in brois)
                  + f" | {npos}/{len(blocks)} pos, worst={min(brois):+.1f} mean={sum(brois)/len(brois):+.1f}")
