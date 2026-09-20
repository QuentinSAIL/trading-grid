#!/usr/bin/env python3
"""CANONICAL final candidate strategy: "Regime Harvest" (two-sleeve).

Sleeve A (wA of capital): conservative volatility-harvest grid (proven
GridBacktester) — near market-neutral, harvests chop, protects in bear.
Sleeve B (1-wA): chandelier trend (Donchian breakout entry + ATR trailing
stop) — long BTC in confirmed uptrends, 100% cash otherwise.

Combined equity = eqA + eqB. This is the single source of truth referenced
by validation experiments.
"""
import os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from engine import simulate_allocation, rolling_max
from runner import metrics_from_equity
from backtest import GridBacktester

WINDOWS = [90, 180, 365, 800]

# Production "Robust Harvester" config (aligned with .env.example / backtest.py).
# Key lever vs the old grid: weight_factor 0.0 (equal per-level sizing).
GRID_PARAMS = dict(
    levels=4, spread=0.010, range_pct=0.04, stop_loss_pct=0.25,
    maker_fee=0.0, taker_fee=0.001, grid_type="geometric", weight_factor=0.0,
    rsi_period=14, rsi_strength=4.0, ema_fast=12, ema_slow=26,
    ema_strength=0.0, bb_period=20, bb_mult=2.0, bb_spread_adapt=True,
    stale_hours=72, decay_per_hour=0.0008, trend_spread_mult=0.0,
    dd_threshold=1.0, dd_factor=0.5, max_inv_ratio=0.30,
    initial_btc_pct=0.25, rebalance_every=1, grid_refresh=0,
    inv_target=0.18, inv_tolerance=0.07, inv_target_max=0.30,
)

# Sleeve B chandelier params
CHAND = dict(entry_lb=168, atr_mult=2.5, atr_lb=48)

DEFAULT_WA = 0.35


def grid_sleeve_equity(candles, capital, taker_fee=0.001, maker_fee=0.0):
    p = dict(GRID_PARAMS); p["taker_fee"] = taker_fee; p["maker_fee"] = maker_fee
    bt = GridBacktester(capital=capital, **p)
    eq = []
    for r in candles.tolist():
        bt.process_candle(r)
        eq.append(bt.capital + bt.btc_held * r[4])
    return np.array(eq), bt.total_trades


def chandelier_alloc(candles, entry_lb=168, atr_mult=2.5, atr_lb=48, smax=1.0):
    o, h, l, cl = candles[:, 1], candles[:, 2], candles[:, 3], candles[:, 4]
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(cl, 1)),
                                      np.abs(l - np.roll(cl, 1))))
    tr[0] = h[0] - l[0]
    atr = np.zeros_like(cl)
    atr[atr_lb] = tr[1:atr_lb + 1].mean()
    for i in range(atr_lb + 1, len(cl)):
        atr[i] = (atr[i - 1] * (atr_lb - 1) + tr[i]) / atr_lb
    hh = rolling_max(cl, entry_lb)
    al = np.zeros_like(cl)
    pos = 0.0; hi = 0.0; trail = 0.0
    for i in range(1, len(cl)):
        if pos == 0.0:
            if cl[i] >= hh[i - 1] and atr[i] > 0:
                pos = smax; hi = cl[i]; trail = cl[i] - atr_mult * atr[i]
        else:
            hi = max(hi, cl[i])
            trail = max(trail, hi - atr_mult * atr[i])
            if cl[i] < trail:
                pos = 0.0
        al[i] = pos
    return al


def trend_sleeve_equity(candles, capital, taker_fee=0.001, band=0.03,
                        slippage=0.0, **chand):
    raw = chandelier_alloc(candles, **{**CHAND, **chand})
    sh = np.roll(raw, 1); sh[0] = 0.0
    eq, tr = simulate_allocation(candles, sh, taker_fee=taker_fee + slippage,
                                 band=band, initial=capital)
    return np.array(eq), tr


def regime_harvest_equity(candles, capital=1000.0, wA=DEFAULT_WA,
                          taker_fee=0.001, maker_fee=0.0, slippage=0.0):
    eqA, trA = grid_sleeve_equity(candles, capital * wA, taker_fee, maker_fee)
    eqB, trB = trend_sleeve_equity(candles, capital * (1 - wA), taker_fee,
                                   slippage=slippage)
    return eqA + eqB, trA + trB


def backtest_windows(symbol="BTC/USDT", exchange="binance", timeframe="1h",
                     wA=DEFAULT_WA, windows=WINDOWS, capital=1000.0,
                     taker_fee=0.001, maker_fee=0.0, slippage=0.0):
    arr = dc.load(exchange, symbol, timeframe)
    end = arr[-1, 0]
    out = {}
    for days in windows:
        sub = arr[arr[:, 0] >= end - days * 86400 * 1000]
        eq, tr = regime_harvest_equity(sub, capital, wA, taker_fee,
                                       maker_fee, slippage)
        m = metrics_from_equity(sub[:, 0], eq, capital, days, sub[0, 1], sub[-1, 4])
        m["trades"] = tr
        out[days] = m
    return out


def print_windows(name, out):
    rois = [out[d]["roi"] for d in sorted(out)]
    allp = all(r > 0 for r in rois)
    print(f"\n=== {name} ===")
    for d in sorted(out):
        m = out[d]
        print(f"  {d:4d}j: ROI={m['roi']:+8.2f}%  maxDD={m['max_dd']:5.1f}%  "
              f"Sharpe={m['sharpe']:+5.2f}  Sortino={m['sortino']:+6.2f}  "
              f"Calmar={m['calmar']:+6.2f}  B&H={m['bh']:+7.1f}%  "
              f"vsHold={m['vs_hold']:+7.1f}%  trades={m['trades']}")
    print(f"  >>> worst={min(rois):+.2f}%  mean={sum(rois)/len(rois):+.2f}%  "
          f"all_positive={allp}")


if __name__ == "__main__":
    for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
        out = backtest_windows(symbol=sym)
        print_windows(f"Regime Harvest wA={DEFAULT_WA} — {sym}", out)
