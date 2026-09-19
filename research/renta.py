#!/usr/bin/env python3
"""Search for HIGHER return: multi-asset portfolio, trend-overlay weight,
regime-scaled inventory. Honest return/risk/OOS reporting."""
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
ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

HARVESTER = dict(GRID_PARAMS)
HARVESTER.update(dict(weight_factor=0.0, max_inv_ratio=0.30, inv_target=0.18))

# realistic MEXC-ish costs
GMAKER, GTAKER, TTAKER, TSLIP = 0.0002, 0.001, 0.001, 0.0005


def grid_eq(candles, capital, params=None):
    p = dict(HARVESTER)
    if params:
        p.update(params)
    p["maker_fee"] = GMAKER; p["taker_fee"] = GTAKER
    bt = GridBacktester(capital=capital, **p)
    eq = np.empty(len(candles))
    for i, r in enumerate(candles.tolist()):
        bt.process_candle(r); eq[i] = bt.capital + bt.btc_held * r[4]
    return eq


def trend_eq(candles, capital, atr_mult=2.5):
    raw = chandelier_alloc(candles, entry_lb=CHAND["entry_lb"], atr_mult=atr_mult,
                           atr_lb=CHAND["atr_lb"])
    sh = np.roll(raw, 1); sh[0] = 0.0
    eq, _ = simulate_allocation(candles, sh, taker_fee=TTAKER + TSLIP, band=0.03,
                               initial=capital)
    return np.array(eq)


def blend_eq(candles, capital, wGrid, atr_mult=2.5, gparams=None):
    eqA = grid_eq(candles, capital * wGrid, gparams)
    if wGrid >= 0.999:
        return eqA
    return eqA + trend_eq(candles, capital * (1 - wGrid), atr_mult)


def align_assets(days):
    """Return list of (asset, sub-array) aligned to same length for a window."""
    subs = {}
    minlen = None
    for a in ASSETS:
        arr = dc.load("binance", a, "1h"); end = arr[-1, 0]
        sub = arr[arr[:, 0] >= end - days * 86400 * 1000]
        subs[a] = sub
        minlen = len(sub) if minlen is None else min(minlen, len(sub))
    return {a: subs[a][-minlen:] for a in ASSETS}, minlen


def portfolio_windows(wGrid, atr_mult=2.5, gparams=None):
    """Equal-weight portfolio of the strategy across ASSETS."""
    out = {}
    for d in WINDOWS:
        subs, n = align_assets(d)
        eq_total = np.zeros(n)
        cap_each = 1000.0 / len(ASSETS)
        # buy&hold ref = equal weight
        bh = np.mean([(subs[a][-1, 4] - subs[a][0, 1]) / subs[a][0, 1] * 100
                      for a in ASSETS])
        for a in ASSETS:
            eq_total += blend_eq(subs[a], cap_each, wGrid, atr_mult, gparams)
        m = metrics_from_equity(subs[ASSETS[0]][:, 0], eq_total, 1000.0, d,
                                1.0, 1.0)
        m["bh"] = bh; m["vs_hold"] = m["roi"] - bh
        out[d] = m
    return out


def single_windows(symbol, wGrid, atr_mult=2.5, gparams=None):
    arr = dc.load("binance", symbol, "1h"); end = arr[-1, 0]; out = {}
    for d in WINDOWS:
        sub = arr[arr[:, 0] >= end - d * 86400 * 1000]
        eq = blend_eq(sub, 1000.0, wGrid, atr_mult, gparams)
        out[d] = metrics_from_equity(sub[:, 0], eq, 1000.0, d, sub[0, 1], sub[-1, 4])
    return out


def blocks(symbol, wGrid, atr_mult=2.5, bd=150, gparams=None):
    arr = dc.load("binance", symbol, "1h"); blk = bd * 86400 * 1000
    t = arr[0, 0]; end = arr[-1, 0]; res = []
    while t + blk <= end + 1:
        sub = arr[(arr[:, 0] >= t) & (arr[:, 0] < t + blk)]
        if len(sub) > bd * 20:
            eq = blend_eq(sub, 1000.0, wGrid, atr_mult, gparams)
            res.append(metrics_from_equity(sub[:, 0], eq, 1000.0, bd, sub[0, 1], sub[-1, 4])["roi"])
        t += blk
    return res


def line(name, out, extra=""):
    rois = [out[d]["roi"] for d in WINDOWS]
    dd = max(out[d]["max_dd"] for d in WINDOWS)
    sh = sum(out[d]["sharpe"] for d in WINDOWS) / 4
    allp = all(r > 0 for r in rois)
    print(f"  {name:34s} " + " ".join(f"{d}:{out[d]['roi']:+6.1f}" for d in WINDOWS)
          + f" | worst={min(rois):+5.1f} mean={sum(rois)/4:+6.1f} maxDD={dd:4.1f} "
          f"Sh={sh:+.2f} {'ALL+' if allp else '   '} {extra}")


if __name__ == "__main__":
    print("### A) SINGLE BTC — trend-overlay weight sweep (realistic costs)")
    for wGrid in [1.0, 0.6, 0.5, 0.35, 0.2]:
        out = single_windows("BTC/USDT", wGrid)
        bl = blocks("BTC/USDT", wGrid); npos = sum(1 for r in bl if r > 0)
        line(f"grid{int(wGrid*100)}%/trend{int((1-wGrid)*100)}%", out,
             f"| OOS {npos}/{len(bl)} worst={min(bl):+.0f}")

    print("\n### B) MULTI-ASSET PORTFOLIO (BTC+ETH+SOL equal weight)")
    for wGrid in [1.0, 0.6, 0.5, 0.35]:
        out = portfolio_windows(wGrid)
        line(f"PORTF grid{int(wGrid*100)}%/trend{int((1-wGrid)*100)}%", out)

    print("\n### C) SINGLE-ASSET harvester-only, per asset (diversification source)")
    for a in ASSETS:
        out = single_windows(a, 1.0)
        line(f"{a} harvester", out)
