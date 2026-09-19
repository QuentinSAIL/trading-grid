#!/usr/bin/env python3
"""Search for a regime/allocation rule that survives the 365d bear AND
captures bulls. Tests many trend filters across all windows/assets."""
import os, sys, math
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import data_cache as dc
from engine import ema, sma, rsi, rolling_max, rolling_min, simulate_allocation
from runner import metrics_from_equity

WINDOWS = [90, 180, 365, 800]


def score_alloc(alloc_fn, exchange="binance", symbol="BTC/USDT", tf="1h",
                taker=0.001, band=0.03):
    arr = dc.load(exchange, symbol, tf)
    end = arr[-1, 0]
    res = {}
    for days in WINDOWS:
        cutoff = end - days * 86400 * 1000
        sub = arr[arr[:, 0] >= cutoff]
        raw = alloc_fn(sub)
        sh = np.roll(raw, 1); sh[0] = 0.0
        eq, tr = simulate_allocation(sub, sh, taker_fee=taker, band=band)
        m = metrics_from_equity(sub[:, 0], eq, 1000.0, days, sub[0, 1], sub[-1, 4])
        m["trades"] = tr
        res[days] = m
    return res


def summ(name, res):
    rois = [res[d]["roi"] for d in WINDOWS]
    shp = [res[d]["sharpe"] for d in WINDOWS]
    allp = all(r > 0 for r in rois)
    print(f"{name:42s} | " + " ".join(f"{d}:{res[d]['roi']:+6.1f}%" for d in WINDOWS)
          + f" | worst={min(rois):+6.1f} mean={sum(rois)/4:+6.1f} "
          f"meanSharpe={sum(shp)/4:+.2f} {'ALL+' if allp else '    '}")
    return {"worst": min(rois), "mean": sum(rois)/4, "allp": allp,
            "meanSharpe": sum(shp)/4}


# ---- allocation rules ----
def a_price_sma_slope(period, slope_lb, smax=1.0):
    def f(c):
        cl = c[:, 4]; m = sma(cl, period)
        slope = np.zeros_like(cl)
        slope[slope_lb:] = (m[slope_lb:] - m[:-slope_lb])
        al = ((cl > m) & (slope > 0)).astype(float) * smax
        al[np.isnan(m)] = 0.0
        return al
    return f


def a_momentum(lb, smax=1.0):
    def f(c):
        cl = c[:, 4]
        mom = np.zeros_like(cl)
        mom[lb:] = (cl[lb:] - cl[:-lb]) / cl[:-lb]
        return (mom > 0).astype(float) * smax
    return f


def a_dual_confirm(sma_p, mom_lb, smax=1.0):
    """Long only if price>SMA AND momentum>0 (both agree)."""
    def f(c):
        cl = c[:, 4]; m = sma(cl, sma_p)
        mom = np.zeros_like(cl); mom[mom_lb:] = (cl[mom_lb:] - cl[:-mom_lb]) / cl[:-mom_lb]
        al = ((cl > m) & (mom > 0)).astype(float) * smax
        al[np.isnan(m)] = 0.0
        return al
    return f


def a_ema_band(fast, slow, band, smax=1.0):
    """EMA cross with hysteresis band to reduce whipsaw."""
    def f(c):
        cl = c[:, 4]; ef = ema(cl, fast); es = ema(cl, slow)
        gap = (ef - es) / es
        al = np.zeros_like(cl); pos = 0.0
        for i in range(len(cl)):
            if gap[i] > band: pos = smax
            elif gap[i] < -band: pos = 0.0
            al[i] = pos
        return al
    return f


def a_chandelier(entry_lb, atr_mult, atr_lb=48, smax=1.0):
    """Breakout entry + ATR trailing-stop exit (chandelier)."""
    def f(c):
        o, h, l, cl = c[:, 1], c[:, 2], c[:, 3], c[:, 4]
        tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(cl, 1)),
                                          np.abs(l - np.roll(cl, 1))))
        tr[0] = h[0] - l[0]
        atr = np.zeros_like(cl); atr[atr_lb] = tr[1:atr_lb+1].mean()
        for i in range(atr_lb+1, len(cl)):
            atr[i] = (atr[i-1]*(atr_lb-1) + tr[i]) / atr_lb
        hh = rolling_max(cl, entry_lb)
        al = np.zeros_like(cl); pos = 0.0; trail = 0.0; hi_since = 0.0
        for i in range(1, len(cl)):
            if pos == 0.0:
                if cl[i] >= hh[i-1] and atr[i] > 0:
                    pos = smax; hi_since = cl[i]; trail = cl[i] - atr_mult*atr[i]
            else:
                hi_since = max(hi_since, cl[i])
                trail = max(trail, hi_since - atr_mult*atr[i])
                if cl[i] < trail:
                    pos = 0.0
            al[i] = pos
        return al
    return f


def a_scaled_trend(fast, slow, gain, smax=1.0):
    """Continuous allocation scaled by EMA gap (not binary)."""
    def f(c):
        cl = c[:, 4]; ef = ema(cl, fast); es = ema(cl, slow)
        gap = (ef - es) / es
        al = np.clip(0.5 + gain * gap, 0, 1) * smax
        return al
    return f


if __name__ == "__main__":
    print("### BTC 1h — allocation rule search (band=0.03, taker=0.1%)\n")
    tests = [
        ("price>SMA480 & slope96", a_price_sma_slope(480, 96)),
        ("price>SMA720 & slope120", a_price_sma_slope(720, 120)),
        ("momentum 30d(720h)", a_momentum(720)),
        ("momentum 21d(504h)", a_momentum(504)),
        ("dual SMA480 & mom504", a_dual_confirm(480, 504)),
        ("dual SMA360 & mom360", a_dual_confirm(360, 360)),
        ("EMA100/400 band1%", a_ema_band(100, 400, 0.01)),
        ("EMA100/400 band2%", a_ema_band(100, 400, 0.02)),
        ("EMA50/200 band2%", a_ema_band(50, 200, 0.02)),
        ("chandelier 168h x3", a_chandelier(168, 3.0)),
        ("chandelier 240h x4", a_chandelier(240, 4.0)),
        ("scaled EMA100/400 g15", a_scaled_trend(100, 400, 15)),
        ("scaled EMA100/400 g25 max0.8", a_scaled_trend(100, 400, 25, 0.8)),
    ]
    ranked = []
    for name, fn in tests:
        res = score_alloc(fn)
        s = summ(name, res)
        ranked.append((name, s))
    print("\n### Ranked by all-positive then worst-window ROI:")
    ranked.sort(key=lambda x: (x[1]["allp"], x[1]["worst"]), reverse=True)
    for name, s in ranked[:8]:
        print(f"  {name:42s} allp={s['allp']} worst={s['worst']:+.1f} "
              f"mean={s['mean']:+.1f} sharpe={s['meanSharpe']:+.2f}")
