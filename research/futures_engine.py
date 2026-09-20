#!/usr/bin/env python3
"""RESULTAT NEGATIF (garde comme evidence) — Market-neutral FUTURES harvester.

Verdict (2026-09): la direction long+short NE BAT PAS le harvester spot long-only.
- Un grid SYMETRIQUE fade les tendances: il vend dans la hausse (short) et perd
  quand ca continue (-15% sur un +27% de bull, meme en version minimale propre).
- Shorter le bear (2024-26, choppy) via trend-following => WHIPSAW: 365j passe de
  +5-7% (harvester spot) a -4.6%, maxDD plus haut.
- Ce moteur a aussi un order-management non borne (counters s'accumulent) qui
  amplifie le blow-up; meme borne, le concept reste structurellement faible.
Conclusion: rester long-only spot; pour +0.2%/j, seul le LEVIER (futures x1.5)
sur le harvester spot est un chemin valide (voir research/lev_validate.py).

--- code d'origine ci-dessous (Market-neutral / trend-scaled long+short) ---
Market-neutral / trend-scaled FUTURES grid harvester (long+short).

Reuses spot-style accounting (equity = cash + pos*price) which is correct for a
linear perp even when pos<0 (short) and cash<0 (borrowed notional). Adds:
- signed position (can go short),
- leverage cap on gross notional,
- perp funding on net notional every 8 bars,
- liquidation on maintenance margin breach,
- trend-scaled net-position target (short in downtrend, long in uptrend, ~0 chop).
"""
import os, sys, math
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import data_cache as dc
from runner import metrics_from_equity


class FuturesHarvester:
    def __init__(self, capital=1000.0, spread=0.010, levels=4,
                 ema_fast=100, ema_slow=400, a_max=0.5, trend_gain=25.0,
                 leverage=1.0, funding_8h=0.0001, maker_fee=0.0002,
                 taker_fee=0.001, rebalance_every=1, vol_window=24,
                 vol_mult=2.0, maint_margin=0.006, grid_frac=None):
        self.cash = capital
        self.pos = 0.0
        self.initial = capital
        self.spread = spread
        self.base_spread = spread
        self.levels = levels
        self.ema_fast_p = ema_fast
        self.ema_slow_p = ema_slow
        self.a_max = a_max
        self.trend_gain = trend_gain
        self.L = leverage
        self.funding_8h = funding_8h
        self.maker = maker_fee
        self.taker = taker_fee
        self.rebalance_every = rebalance_every
        self.vol_window = vol_window
        self.vol_mult = vol_mult
        self.maint = maint_margin
        self.grid_frac = grid_frac if grid_frac is not None else leverage
        self.closes = []
        self._ef = self._es = None
        self.trend = 0.0
        self.orders = []
        self.total_trades = 0
        self.stopped = False
        self._n = 0
        self.base_price = None

    def equity(self, price):
        return self.cash + self.pos * price

    def _update_ema(self):
        c = self.closes[-1]
        if self._ef is None:
            if len(self.closes) >= self.ema_slow_p:
                self._ef = np.mean(self.closes[-self.ema_fast_p:])
                self._es = np.mean(self.closes[-self.ema_slow_p:])
            return
        kf = 2 / (self.ema_fast_p + 1); ks = 2 / (self.ema_slow_p + 1)
        self._ef = c * kf + self._ef * (1 - kf)
        self._es = c * ks + self._es * (1 - ks)
        if self._es > 0:
            self.trend = (self._ef - self._es) / self._es

    def _update_spread(self):
        w = self.closes[-self.vol_window:]
        if len(w) < 3:
            return
        r = np.diff(w) / np.asarray(w[:-1])
        target = float(np.std(r)) * self.vol_mult
        target = max(self.base_spread, min(self.base_spread * 4, target))
        self.spread = self.spread * 0.7 + target * 0.3

    def _target_pos(self, price, equity):
        """Signed net position target (coin). a_max=0 => market neutral."""
        if self._es is None or self.a_max == 0:
            return 0.0
        a = max(-1.0, min(1.0, self.trend * self.trend_gain))
        target_notional = a * self.a_max * self.L * equity
        return target_notional / price

    def _place_grid(self, price, equity):
        self.orders = [o for o in self.orders if o.get("is_counter")]
        gross = self.grid_frac * equity
        per_level_notional = (gross / (2 * self.levels))
        s = self.spread
        for i in range(1, self.levels + 1):
            bp = price * (1 - s) ** i
            self.orders.append({"side": "buy", "price": bp,
                                "size": per_level_notional / bp,
                                "is_counter": False})
            sp = price * (1 + s) ** i
            self.orders.append({"side": "sell", "price": sp,
                                "size": per_level_notional / sp,
                                "is_counter": False})

    def _fill(self, h, l):
        filled = [o for o in self.orders
                  if (o["side"] == "buy" and l <= o["price"])
                  or (o["side"] == "sell" and h >= o["price"])]
        for o in filled:
            self.orders.remove(o)
            fp = o["price"]; s = o["size"]
            is_counter = o.get("is_counter")
            eq = self.cash + self.pos * fp
            # hard cap on net position magnitude: initials that would push |pos|
            # beyond grid capacity are skipped (bounds exposure like a real grid).
            pos_cap = self.grid_frac * eq / fp if fp > 0 else 0
            fee = fp * s * self.maker
            if o["side"] == "buy":
                if not is_counter and self.pos >= pos_cap:
                    continue
                self.cash -= fp * s + fee
                self.pos += s
                self.orders.append({"side": "sell", "price": fp * (1 + self.spread),
                                    "size": s, "is_counter": True})
            else:
                if not is_counter and self.pos <= -pos_cap:
                    continue
                self.cash += fp * s - fee
                self.pos -= s
                self.orders.append({"side": "buy", "price": fp * (1 - self.spread),
                                    "size": s, "is_counter": True})
            self.total_trades += 1

    def _rebalance(self, price, equity):
        tgt = self._target_pos(price, equity)
        delta = tgt - self.pos
        # cap gross notional to leverage
        if abs(delta) * price < equity * 0.02:
            return
        fee = abs(delta) * price * self.taker
        self.cash -= delta * price + fee   # buy delta>0: cash down; sell: cash up
        self.pos += delta
        self.total_trades += 1

    def _cap_leverage(self, price):
        eq = self.equity(price)
        gross = abs(self.pos) * price
        if eq > 0 and gross > self.L * eq * 1.05:
            # trim position to leverage cap (taker)
            target_gross = self.L * eq
            new_abs = target_gross / price
            reduce = abs(self.pos) - new_abs
            if reduce > 0:
                sign = 1 if self.pos > 0 else -1
                self.cash += sign * reduce * price - reduce * price * self.taker
                self.pos -= sign * reduce

    def process_candle(self, row):
        ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        if self.stopped:
            return
        self._n += 1
        if self.closes:
            self._fill(h, l)
        self.closes.append(c)
        self._update_ema()
        self._update_spread()
        eq = self.equity(c)
        # funding every 8 bars (longs pay when funding>0)
        if self._n % 8 == 0 and self.pos != 0:
            self.cash -= self.funding_8h * self.pos * c
        # liquidation
        eq = self.equity(c)
        if eq <= self.maint * abs(self.pos) * c or eq <= 0:
            self.cash = max(0.0, eq); self.pos = 0.0
            self.orders = []; self.stopped = True
            return
        if self.base_price is None:
            self.base_price = c
        if self._es is None:
            return
        self._cap_leverage(c)
        if self.rebalance_every > 0 and self._n % self.rebalance_every == 0:
            self._rebalance(c, eq)
        # refresh grid periodically / on drift
        drift = abs(c - self.base_price) / self.base_price
        if self._n % max(1, self.rebalance_every) == 0 or drift > 0.04:
            self._place_grid(c, eq)
            self.base_price = c


# ---- backtest harness ----
WINDOWS = [90, 180, 365, 800]
PORT7 = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "LINK/USDT", "LTC/USDT"]
_C = {}
def arr_for(a):
    if a not in _C:
        _C[a] = dc.load("binance", a, "1h")
    return _C[a]

def run_asset(candles, **kw):
    fh = FuturesHarvester(capital=1000.0 / len(PORT7), **kw)
    eq = np.empty(len(candles))
    for i, r in enumerate(candles.tolist()):
        fh.process_candle(r); eq[i] = fh.equity(r[4])
    return eq

def port_eq(assets, days, **kw):
    subs = {a: arr_for(a)[arr_for(a)[:, 0] >= arr_for(a)[-1, 0] - days * 86400 * 1000] for a in assets}
    n = min(len(subs[a]) for a in assets)
    subs = {a: subs[a][-n:] for a in assets}
    eq = np.zeros(n)
    for a in assets:
        eq += run_asset(subs[a], **kw)
    return eq, subs[assets[0]][:, 0]

def blocks_eq(assets, bd=150, **kw):
    a0 = arr_for(assets[0]); blk = bd * 86400 * 1000; t = a0[0, 0]; end = a0[-1, 0]
    res = []
    while t + blk <= end + 1:
        subs = {a: arr_for(a)[(arr_for(a)[:, 0] >= t) & (arr_for(a)[:, 0] < t + blk)] for a in assets}
        n = min(len(subs[a]) for a in assets)
        if n > bd * 20:
            subs = {a: subs[a][-n:] for a in assets}
            eq = np.zeros(n)
            for a in assets:
                eq += run_asset(subs[a], **kw)
            res.append(metrics_from_equity(subs[assets[0]][:, 0], eq, 1000.0, bd, 1.0, 1.0)["roi"])
        t += blk
    return res

def report(label, **kw):
    row = {}
    for d in WINDOWS:
        eq, ts = port_eq(PORT7, d, **kw)
        row[d] = metrics_from_equity(ts, eq, 1000.0, d, 1.0, 1.0)
    blk = blocks_eq(PORT7, **kw)
    dr = {d: row[d]["roi"] / d for d in WINDOWS}
    dd = max(row[d]["max_dd"] for d in WINDOWS)
    allp = all(row[d]["roi"] > 0 for d in WINDOWS)
    npos = sum(1 for b in blk if b > 0)
    print(f"  {label:34s} " + " ".join(f"{d}:{dr[d]:+.3f}/j" for d in WINDOWS)
          + f" | 800d={dr[800]:+.3f} min={min(dr.values()):+.3f} maxDD={dd:4.1f}% "
          f"{'ALL+' if allp else '    '} | OOS {npos}/{len(blk)} worst={min(blk):+.0f}%")


if __name__ == "__main__":
    print("### FUTURES harvester — CONSERVATIVE grid_frac, rebalance_every=6\n")
    print("# market-neutral, small grid:")
    report("MN gf0.3 reb6", a_max=0.0, leverage=1.0, grid_frac=0.3, rebalance_every=6)
    report("MN gf0.2 reb6", a_max=0.0, leverage=1.0, grid_frac=0.2, rebalance_every=6)
    print("# trend-scaled bias, small grid:")
    report("trend a0.5 gf0.3 reb6", a_max=0.5, leverage=1.0, grid_frac=0.3, rebalance_every=6)
    report("trend a1.0 gf0.3 reb6", a_max=1.0, leverage=1.0, grid_frac=0.3, rebalance_every=6)
