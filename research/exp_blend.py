#!/usr/bin/env python3
"""Two-sleeve blend: (A) conservative harvest grid + (B) chandelier trend.
Combined equity = wA*eqA + wB*eqB. Test across assets/windows."""
import os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from engine import simulate_allocation
from exp_trend import a_chandelier
from runner import metrics_from_equity
from backtest import GridBacktester

WINDOWS = [90, 180, 365, 800]

GRID_PARAMS = dict(
    levels=3, spread=0.008, range_pct=0.04, stop_loss_pct=0.25,
    maker_fee=0.0, taker_fee=0.001, grid_type="geometric", weight_factor=-0.9,
    rsi_period=14, rsi_strength=4.0, ema_fast=12, ema_slow=26,
    ema_strength=0.0, bb_period=20, bb_mult=2.0, bb_spread_adapt=True,
    stale_hours=72, decay_per_hour=0.0008, trend_spread_mult=0.0,
    dd_threshold=1.0, dd_factor=0.5, max_inv_ratio=0.20,
    initial_btc_pct=0.25, rebalance_every=1, grid_refresh=0,
    inv_target=0.18, inv_tolerance=0.07,
)


def grid_equity(candles, capital):
    bt = GridBacktester(capital=capital, **GRID_PARAMS)
    eq = []
    for r in candles.tolist():
        bt.process_candle(r)
        eq.append(bt.capital + bt.btc_held * r[4])
    return np.array(eq)


def chandelier_equity(candles, capital, entry_lb, atr_mult, amax=1.0, taker=0.001):
    raw = a_chandelier(entry_lb, atr_mult, smax=amax)(candles)
    sh = np.roll(raw, 1); sh[0] = 0.0
    eq, tr = simulate_allocation(candles, sh, taker_fee=taker, band=0.03, initial=capital)
    return np.array(eq)


def test_blend(wA, entry_lb, atr_mult, amax=1.0, symbol="BTC/USDT", exchange="binance"):
    arr = dc.load(exchange, symbol, "1h")
    end = arr[-1, 0]
    rois = []
    out = {}
    for days in WINDOWS:
        cutoff = end - days * 86400 * 1000
        sub = arr[arr[:, 0] >= cutoff]
        eqA = grid_equity(sub, 1000.0 * wA)
        eqB = chandelier_equity(sub, 1000.0 * (1 - wA), entry_lb, atr_mult, amax)
        eq = eqA + eqB
        m = metrics_from_equity(sub[:, 0], eq, 1000.0, days, sub[0, 1], sub[-1, 4])
        out[days] = m
        rois.append(m["roi"])
    return out, rois


if __name__ == "__main__":
    configs = [
        (0.5, 168, 2.5, 1.0),
        (0.5, 168, 3.0, 1.0),
        (0.4, 168, 2.5, 1.0),
        (0.6, 168, 2.5, 1.0),
        (0.5, 336, 2.5, 1.0),
        (0.5, 120, 2.5, 1.0),
        (0.3, 168, 2.5, 1.0),
    ]
    for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
        print(f"\n##### {sym} #####")
        for wA, e, am, amax in configs:
            out, rois = test_blend(wA, e, am, amax, symbol=sym)
            allp = all(r > 0 for r in rois)
            shp = sum(out[d]["sharpe"] for d in WINDOWS) / 4
            dd = max(out[d]["max_dd"] for d in WINDOWS)
            print(f"  gridW={wA} chand{e}x{am}: " +
                  " ".join(f"{d}:{out[d]['roi']:+6.1f}%" for d in WINDOWS) +
                  f" | worst={min(rois):+6.1f} mean={sum(rois)/4:+6.1f} "
                  f"maxDD={dd:4.1f} Sh={shp:+.2f} {'ALL+' if allp else ''}")
