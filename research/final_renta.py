#!/usr/bin/env python3
"""Honest frontier for the 0.2%/day target: broad universe vs leverage."""
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
PORT10 = PORT7 + ["DOGE/USDT", "AVAX/USDT", "ADA/USDT"]

HARV = dict(GRID_PARAMS)
HARV.update(dict(weight_factor=0.0, max_inv_ratio=0.30, inv_target=0.18,
                 inv_target_max=0.30, spread=0.013, levels=4,
                 maker_fee=0.0002, taker_fee=0.001))

_C = {}
def sub_for(a, d):
    if a not in _C:
        _C[a] = dc.load("binance", a, "1h")
    arr = _C[a]; return arr[arr[:, 0] >= arr[-1, 0] - d * 86400 * 1000]

def grid_eq(candles, cap):
    bt = GridBacktester(capital=cap, **HARV)
    eq = np.empty(len(candles))
    for i, r in enumerate(candles.tolist()):
        bt.process_candle(r); eq[i] = bt.capital + bt.btc_held * r[4]
    return eq

def port_eq(assets, d):
    subs = {a: sub_for(a, d) for a in assets}
    n = min(len(subs[a]) for a in assets)
    subs = {a: subs[a][-n:] for a in assets}
    eq = np.zeros(n)
    for a in assets:
        eq += grid_eq(subs[a], 1000.0 / len(assets))
    return eq, subs[assets[0]][:, 0]

def lever(eq, L):
    """Constant-leverage proxy on per-bar returns. Liquidation if a bar
    return <= -1/L (equity wiped)."""
    r = np.diff(eq) / eq[:-1]
    out = np.empty(len(eq)); out[0] = 1000.0
    liq = False
    for i in range(1, len(eq)):
        rr = L * r[i - 1]
        if rr <= -1.0:
            out[i:] = 0.0; liq = True; break
        out[i] = out[i - 1] * (1 + rr)
    return out, liq

def report(name, assets, L=1.0):
    row = {}
    liq_any = False
    for d in WINDOWS:
        eq, ts = port_eq(assets, d)
        if L != 1.0:
            eq, liq = lever(eq, L); liq_any = liq_any or liq
        m = metrics_from_equity(ts, eq, 1000.0, d, 1.0, 1.0)
        row[d] = m
    dr = {d: row[d]["roi"] / d for d in WINDOWS}
    dd = max(row[d]["max_dd"] for d in WINDOWS)
    allp = all(row[d]["roi"] > 0 for d in WINDOWS)
    tag = "ALL+" if allp else "    "
    liqs = " LIQUIDATED!" if liq_any else ""
    print(f"  {name:26s} " + " ".join(f"{d}:{dr[d]:+.3f}/j" for d in WINDOWS)
          + f" | minDaily={min(dr.values()):+.3f} 800dDaily={dr[800]:+.3f} maxDD={dd:4.1f} {tag}{liqs}")


if __name__ == "__main__":
    print("### Daily-ROI frontier (target >= 0.200%/j). Harvester spread1.3% lv4.\n")
    print("# UNIVERSE (no leverage):")
    report("7 assets", PORT7)
    report("10 assets (+DOGE,AVAX,ADA)", PORT10)
    print("\n# LEVERAGE on 7-asset portfolio (models futures; funding ignored):")
    for L in [1.0, 1.5, 2.0, 3.0]:
        report(f"7 assets x{L}", PORT7, L)
    print("\n# LEVERAGE on 10-asset portfolio:")
    for L in [1.5, 2.0]:
        report(f"10 assets x{L}", PORT10, L)
