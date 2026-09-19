#!/usr/bin/env python3
"""Multi-asset harvester portfolio: does diversification lift return/Sharpe?"""
import os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from runner import metrics_from_equity
from backtest import GridBacktester
from strategy import GRID_PARAMS

WINDOWS = [90, 180, 365, 800]
ALL = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
       "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "ADA/USDT"]

HARVESTER = dict(GRID_PARAMS)
HARVESTER.update(dict(weight_factor=0.0, max_inv_ratio=0.30, inv_target=0.18,
                      maker_fee=0.0002, taker_fee=0.001))


def grid_eq(candles, capital):
    bt = GridBacktester(capital=capital, **HARVESTER)
    eq = np.empty(len(candles))
    for i, r in enumerate(candles.tolist()):
        bt.process_candle(r); eq[i] = bt.capital + bt.btc_held * r[4]
    return eq


def sub_for(asset, days):
    arr = dc.load("binance", asset, "1h"); end = arr[-1, 0]
    return arr[arr[:, 0] >= end - days * 86400 * 1000]


def per_asset(asset):
    out = {}
    for d in WINDOWS:
        sub = sub_for(asset, d)
        eq = grid_eq(sub, 1000.0)
        out[d] = metrics_from_equity(sub[:, 0], eq, 1000.0, d, sub[0, 1], sub[-1, 4])
    return out


def portfolio(assets, days, weights=None):
    subs = {a: sub_for(a, days) for a in assets}
    minlen = min(len(subs[a]) for a in assets)
    subs = {a: subs[a][-minlen:] for a in assets}
    if weights is None:
        weights = {a: 1.0 / len(assets) for a in assets}
    eq_total = np.zeros(minlen)
    bh = 0.0
    for a in assets:
        eq_total += grid_eq(subs[a], 1000.0 * weights[a])
        bh += weights[a] * (subs[a][-1, 4] - subs[a][0, 1]) / subs[a][0, 1] * 100
    m = metrics_from_equity(subs[assets[0]][:, 0], eq_total, 1000.0, days, 1.0, 1.0)
    m["bh"] = bh; m["vs_hold"] = m["roi"] - bh
    return m


def show(name, outfn):
    outs = {d: outfn(d) for d in WINDOWS}
    rois = [outs[d]["roi"] for d in WINDOWS]
    dd = max(outs[d]["max_dd"] for d in WINDOWS)
    sh = sum(outs[d]["sharpe"] for d in WINDOWS) / 4
    allp = all(r > 0 for r in rois)
    print(f"  {name:26s} " + " ".join(f"{d}:{outs[d]['roi']:+6.1f}" for d in WINDOWS)
          + f" | worst={min(rois):+5.1f} mean={sum(rois)/4:+6.1f} maxDD={dd:4.1f} "
          f"Sh={sh:+.2f} {'ALL+' if allp else '   '}")


if __name__ == "__main__":
    print("### PER-ASSET harvester (which alts are safe to include?)")
    good = []
    for a in ALL:
        o = per_asset(a)
        rois = [o[d]["roi"] for d in WINDOWS]
        allp = all(r > 0 for r in rois)
        dd = max(o[d]["max_dd"] for d in WINDOWS)
        tag = "ALL+" if allp else f"(worst {min(rois):+.0f})"
        print(f"  {a:12s} " + " ".join(f"{d}:{o[d]['roi']:+6.1f}" for d in WINDOWS)
              + f" | mean={sum(rois)/4:+6.1f} maxDD={dd:4.1f} {tag}")
        if allp:
            good.append(a)
    print(f"\n  all-positive assets: {good}")

    print("\n### PORTFOLIOS (equal weight, harvester-only)")
    ports = {
        "3 (BTC,ETH,SOL)": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "5 majors": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"],
        "7 diversified": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
                          "XRP/USDT", "LINK/USDT", "LTC/USDT"],
        "10 all": ALL,
    }
    for name, assets in ports.items():
        show(name, lambda d, a=assets: portfolio(a, d))

    print("\n### PORTFOLIO of only all-positive-per-asset names (equal weight)")
    if good:
        show(f"{len(good)} safe", lambda d: portfolio(good, d))

    print("\n### INVERSE-VOL weighted (risk parity) — 7 diversified")
    sevens = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
              "XRP/USDT", "LINK/USDT", "LTC/USDT"]
    def invvol_port(d):
        subs = {a: sub_for(a, d) for a in sevens}
        vols = {}
        for a in sevens:
            c = subs[a][:, 4]; r = np.diff(c) / c[:-1]
            vols[a] = r.std() if r.std() > 0 else 1.0
        inv = {a: 1.0 / vols[a] for a in sevens}
        tot = sum(inv.values())
        w = {a: inv[a] / tot for a in sevens}
        return portfolio(sevens, d, weights=w)
    show("7 inv-vol", invvol_port)
