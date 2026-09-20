#!/usr/bin/env python3
"""Rigorous validation of the LEVERAGED harvester portfolio (futures reality):
funding cost on net-long notional, walk-forward OOS, liquidation headroom."""
import os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from runner import metrics_from_equity
from backtest import GridBacktester
from strategy import GRID_PARAMS

WINDOWS = [90, 180, 365, 800]
PORT7 = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "LINK/USDT", "LTC/USDT"]

HARV = dict(GRID_PARAMS)
HARV.update(dict(weight_factor=0.0, max_inv_ratio=0.30, inv_target=0.18,
                 inv_target_max=0.30, spread=0.013, levels=4,
                 maker_fee=0.0002, taker_fee=0.001))

_C = {}
def arr_for(a):
    if a not in _C:
        _C[a] = dc.load("binance", a, "1h")
    return _C[a]

def eq_and_coin(candles, cap):
    """Return per-bar equity and coin notional value."""
    bt = GridBacktester(capital=cap, **HARV)
    n = len(candles); eq = np.empty(n); coin = np.empty(n)
    for i, r in enumerate(candles.tolist()):
        bt.process_candle(r)
        coin[i] = bt.btc_held * r[4]
        eq[i] = bt.capital + coin[i]
    return eq, coin

def port_series(assets, candle_slice):
    """Aligned portfolio equity + coin-fraction per bar for a time slice fn."""
    subs = {a: candle_slice(a) for a in assets}
    n = min(len(subs[a]) for a in assets)
    subs = {a: subs[a][-n:] for a in assets}
    eq = np.zeros(n); coin = np.zeros(n)
    for a in assets:
        e, c = eq_and_coin(subs[a], 1000.0 / len(assets))
        eq += e; coin += c
    cf = np.divide(coin, eq, out=np.zeros(n), where=eq > 0)
    return eq, cf, subs[assets[0]][:, 0]

def apply_leverage(eq, cf, L, funding_8h):
    """Constant leverage L with perp funding on net-long notional (every 8 bars).
    Returns levered equity and liquidation flag."""
    n = len(eq)
    r = np.zeros(n)
    r[1:] = (eq[1:] - eq[:-1]) / eq[:-1]
    out = np.empty(n); out[0] = 1000.0; liq = False
    for i in range(1, n):
        rr = L * r[i]
        if i % 8 == 0:                      # funding event every 8h
            rr -= funding_8h * L * cf[i]     # long pays funding on levered notional
        if rr <= -1.0:
            out[i:] = 0.0; liq = True; break
        out[i] = out[i - 1] * (1 + rr)
    return out, liq

def worst_bar(eq):
    r = np.diff(eq) / eq[:-1]
    return r.min() * 100

# Precompute spot series ONCE (expensive), reuse for all (L, funding) combos.
def precompute():
    win = {}
    for d in WINDOWS:
        win[d] = port_series(PORT7, lambda a, d=d: arr_for(a)[arr_for(a)[:, 0] >= arr_for(a)[-1, 0] - d * 86400 * 1000])
    a0 = arr_for(PORT7[0]); start = a0[0, 0]; end = a0[-1, 0]; blk = 150 * 86400 * 1000
    blocks = []; t = start
    while t + blk <= end + 1:
        def sl(a, t=t):
            x = arr_for(a); return x[(x[:, 0] >= t) & (x[:, 0] < t + blk)]
        try:
            blocks.append(port_series(PORT7, sl))
        except Exception:
            pass
        t += blk
    return win, blocks


def validate(L, funding_8h, win, blocks):
    row = {}
    for d in WINDOWS:
        eq, cf, ts = win[d]
        lev, liq = apply_leverage(eq, cf, L, funding_8h)
        m = metrics_from_equity(ts, lev, 1000.0, d, 1.0, 1.0)
        m["liq"] = liq; m["wb"] = worst_bar(eq)
        row[d] = m
    broi = []
    for eq, cf, ts in blocks:
        lev, _ = apply_leverage(eq, cf, L, funding_8h)
        broi.append(metrics_from_equity(ts, lev, 1000.0, 150, 1.0, 1.0)["roi"])
    return row, broi


if __name__ == "__main__":
    print("### LEVERAGED harvester validation — funding on net-long notional + walk-forward\n")
    WIN, BLOCKS = precompute()
    for L in [1.5, 2.0]:
        for f8 in [0.0001, 0.0003, 0.0005]:
            row, blocks = validate(L, f8, WIN, BLOCKS)
            dr = {d: row[d]["roi"] / d for d in WINDOWS}
            dd = max(row[d]["max_dd"] for d in WINDOWS)
            allp = all(row[d]["roi"] > 0 for d in WINDOWS)
            liq = any(row[d]["liq"] for d in WINDOWS)
            npos = sum(1 for b in blocks if b > 0)
            wb = min(row[d]["wb"] for d in WINDOWS)
            print(f"L={L} funding={f8*100:.2f}%/8h ({f8*3*100:.2f}%/j): "
                  f"800dDaily={dr[800]:+.3f}/j minDaily={min(dr.values()):+.3f}/j "
                  f"maxDD={dd:4.1f}% {'ALL+' if allp else '    '} "
                  f"| OOS {npos}/{len(blocks)} worst={min(blocks):+.1f}% "
                  f"| worstBar={wb:.1f}%(x{L}={wb*L:.0f}%) {'LIQ!' if liq else ''}")
        print()
