#!/usr/bin/env python3
"""Vectorized, look-ahead-safe allocation simulator + signal library.

An "allocation strategy" produces target_alloc[t] in [0,1] = fraction of
portfolio to hold in the coin at bar t, decided from data up to bar t-1
(shifted to avoid look-ahead), executed at close[t] with taker fees and a
rebalance tolerance band (to limit turnover).
"""
import os
import sys
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import data_cache as dc


# ---------------- indicators (vectorized) ----------------
def ema(x, period):
    x = np.asarray(x, dtype=np.float64)
    k = 2.0 / (period + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def sma(x, period):
    x = np.asarray(x, dtype=np.float64)
    out = np.full_like(x, np.nan)
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period
    return out


def rsi(closes, period=14):
    closes = np.asarray(closes, dtype=np.float64)
    delta = np.diff(closes, prepend=closes[0])
    gain = np.clip(delta, 0, None)
    loss = np.clip(-delta, 0, None)
    ag = np.zeros_like(closes)
    al = np.zeros_like(closes)
    ag[period] = gain[1:period + 1].mean()
    al[period] = loss[1:period + 1].mean()
    for i in range(period + 1, len(closes)):
        ag[i] = (ag[i - 1] * (period - 1) + gain[i]) / period
        al[i] = (al[i - 1] * (period - 1) + loss[i]) / period
    rs = np.divide(ag, al, out=np.full_like(ag, 100.0), where=al != 0)
    out = 100 - 100 / (1 + rs)
    out[:period] = 50.0
    return out


def rolling_max(x, w):
    x = np.asarray(x, dtype=np.float64)
    out = np.full_like(x, np.nan)
    for i in range(len(x)):
        lo = max(0, i - w + 1)
        out[i] = x[lo:i + 1].max()
    return out


def rolling_min(x, w):
    x = np.asarray(x, dtype=np.float64)
    out = np.full_like(x, np.nan)
    for i in range(len(x)):
        lo = max(0, i - w + 1)
        out[i] = x[lo:i + 1].min()
    return out


def realized_vol(closes, w=24):
    closes = np.asarray(closes, dtype=np.float64)
    rets = np.diff(closes, prepend=closes[0]) / np.roll(closes, 1)
    rets[0] = 0
    out = np.full_like(closes, np.nan)
    for i in range(len(closes)):
        lo = max(0, i - w + 1)
        out[i] = rets[lo:i + 1].std()
    return out


# ---------------- core simulator ----------------
def simulate_allocation(candles, target_alloc, taker_fee=0.001,
                        band=0.05, initial=1000.0, maker_fee=0.0):
    """candles: Nx6 array. target_alloc: length-N array in [0,1], already
    look-ahead-safe. Rebalance at close[t] toward target if |cur-target|>band."""
    closes = candles[:, 4]
    n = len(closes)
    cash = initial
    coin = 0.0
    trades = 0
    equity = np.empty(n)
    ta = np.clip(np.nan_to_num(target_alloc, nan=0.0), 0.0, 1.0)
    for t in range(n):
        p = closes[t]
        port = cash + coin * p
        cur_alloc = (coin * p) / port if port > 0 else 0.0
        tgt = ta[t]
        if abs(tgt - cur_alloc) > band:
            target_coin_val = port * tgt
            delta_val = target_coin_val - coin * p  # >0 buy, <0 sell
            if delta_val > 0:
                buy_val = min(delta_val, cash)
                fee = buy_val * taker_fee
                coin += (buy_val - fee) / p
                cash -= buy_val
                trades += 1
            elif delta_val < 0:
                sell_val = min(-delta_val, coin * p)
                fee = sell_val * taker_fee
                coin -= sell_val / p
                cash += sell_val - fee
                trades += 1
        equity[t] = cash + coin * p
    return equity, trades


def simulate_alloc_signal(candles, signal_fn, **kw):
    """signal_fn(candles) -> raw alloc array (unshifted). We shift by 1."""
    raw = signal_fn(candles)
    shifted = np.roll(raw, 1)
    shifted[0] = 0.0
    return simulate_allocation(candles, shifted, **kw)


# ---------------- signal library (return alloc in [0,1]) ----------------
def sig_buyhold(candles):
    return np.ones(len(candles))


def sig_price_vs_sma(candles, period=200):
    closes = candles[:, 4]
    m = sma(closes, period)
    alloc = (closes > m).astype(float)
    alloc[np.isnan(m)] = 0.0
    return alloc


def sig_ema_cross(candles, fast=50, slow=200):
    closes = candles[:, 4]
    ef, es = ema(closes, fast), ema(closes, slow)
    return (ef > es).astype(float)


def sig_donchian(candles, entry=100, exit=50):
    """Long when close breaks entry-period high; flat when breaks exit-period low."""
    closes = candles[:, 4]
    hh = rolling_max(closes, entry)
    ll = rolling_min(closes, exit)
    alloc = np.zeros(len(closes))
    pos = 0.0
    for i in range(len(closes)):
        if i > 0:
            if closes[i] >= hh[i - 1] if i >= 1 else False:
                pos = 1.0
            elif closes[i] <= ll[i - 1] if i >= 1 else False:
                pos = 0.0
        alloc[i] = pos
    return alloc


if __name__ == "__main__":
    WINDOWS = [90, 180, 365, 800]
    from runner import metrics_from_equity
    arr = dc.load("binance", "BTC/USDT", "1h")
    end = arr[-1, 0]
    sigs = {
        "buyhold": sig_buyhold,
        "price>SMA200": lambda c: sig_price_vs_sma(c, 200),
        "price>SMA100": lambda c: sig_price_vs_sma(c, 100),
        "EMA50/200": lambda c: sig_ema_cross(c, 50, 200),
        "EMA100/400": lambda c: sig_ema_cross(c, 100, 400),
        "Donchian100/50": lambda c: sig_donchian(c, 100, 50),
    }
    for name, fn in sigs.items():
        print(f"\n=== {name} ===")
        rois = []
        for days in WINDOWS:
            cutoff = end - days * 86400 * 1000
            sub = arr[arr[:, 0] >= cutoff]
            eq, tr = simulate_alloc_signal(sub, fn, taker_fee=0.001, band=0.02)
            m = metrics_from_equity(sub[:, 0], eq, 1000.0, days,
                                    sub[0, 1], sub[-1, 4])
            rois.append(m["roi"])
            print(f"  {days:4d}j: ROI={m['roi']:+8.2f}%  maxDD={m['max_dd']:5.1f}%  "
                  f"Sharpe={m['sharpe']:+5.2f}  Calmar={m['calmar']:+6.2f}  "
                  f"B&H={m['bh']:+7.1f}%  vsHold={m['vs_hold']:+7.1f}%  trades={tr}")
        print(f"  >>> worst={min(rois):+.2f}%  mean={sum(rois)/len(rois):+.2f}%  "
              f"all_positive={all(r>0 for r in rois)}")
