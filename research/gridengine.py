#!/usr/bin/env python3
"""Clean event-driven grid engine with trend-adaptive inventory target.

Idea: ONE coin balance. A slow trend signal sets a *base allocation* a_base
in [a_min, a_max]. A mean-reversion grid of limit orders scalps oscillations
around that base. In downtrends a_base -> ~0 (cash-heavy, capital protected,
still scalping); in uptrends a_base high (ride the bull + scalp).

Compatible with runner.bench: exposes .process_candle(row) and .equity(price).
"""
import os
import sys
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class TrendGrid:
    def __init__(self,
                 capital=1000.0,
                 # trend / allocation
                 ema_fast=100, ema_slow=400, regime_sma=400,
                 a_min=0.0, a_max=0.85, alloc_gain=25.0,
                 slope_lookback=48, slope_min=0.0,
                 # grid
                 levels=6, base_spread=0.006, vol_window=24,
                 vol_mult=2.0, spread_floor_mult=1.0, spread_ceil_mult=5.0,
                 grid_span=0.35,      # fraction of portfolio the grid may swing
                 rebalance_band=0.08, rebalance_every=6,
                 grid_refresh=6,
                 # risk
                 maker_fee=0.0, taker_fee=0.001,
                 hard_stop_trend=None,   # if trend below this, force a_base=0
                 ):
        self.cash = capital
        self.coin = 0.0
        self.initial = capital
        self.ema_fast_p = ema_fast
        self.ema_slow_p = ema_slow
        self.regime_sma_p = regime_sma
        self.a_min = a_min
        self.a_max = a_max
        self.alloc_gain = alloc_gain
        self.slope_lookback = slope_lookback
        self.slope_min = slope_min
        self.levels = levels
        self.base_spread = base_spread
        self.vol_window = vol_window
        self.vol_mult = vol_mult
        self.spread_floor_mult = spread_floor_mult
        self.spread_ceil_mult = spread_ceil_mult
        self.grid_span = grid_span
        self.rebalance_band = rebalance_band
        self.rebalance_every = rebalance_every
        self.grid_refresh = grid_refresh
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.hard_stop_trend = hard_stop_trend

        self.closes = []
        self._ef = None
        self._es = None
        self.trend = 0.0
        self.orders = []      # list of dicts
        self.total_trades = 0
        self.total_profit = 0.0
        self.cycles = 0
        self._n = 0
        self.base_price = None
        self.spread = base_spread
        self.a_base = 0.0

    # ---- indicators ----
    def _update_emas(self):
        c = self.closes[-1]
        if self._ef is None:
            if len(self.closes) >= self.ema_slow_p:
                self._ef = np.mean(self.closes[-self.ema_fast_p:])
                self._es = np.mean(self.closes[-self.ema_slow_p:])
            return
        kf = 2 / (self.ema_fast_p + 1)
        ks = 2 / (self.ema_slow_p + 1)
        self._ef = c * kf + self._ef * (1 - kf)
        self._es = c * ks + self._es * (1 - ks)
        if self._es > 0:
            self.trend = (self._ef - self._es) / self._es

    def _volatility(self):
        w = self.closes[-self.vol_window:]
        if len(w) < 3:
            return 0.0
        r = np.diff(w) / np.asarray(w[:-1])
        return float(np.std(r))

    def _slope_ok(self):
        if len(self.closes) <= self.slope_lookback:
            return True
        past = self.closes[-self.slope_lookback - 1]
        now = self.closes[-1]
        return (now - past) / past >= self.slope_min

    def _target_alloc(self):
        """Base allocation from trend, in [a_min, a_max]."""
        if self._es is None:
            return 0.0
        if self.hard_stop_trend is not None and self.trend < self.hard_stop_trend:
            return 0.0
        # normalized trend score
        t = max(-0.05, min(0.05, self.trend))
        score = 0.5 + self.alloc_gain * t   # linear map; gain controls steepness
        score = max(0.0, min(1.0, score))
        # require positive slope to hold coin (avoid catching knives)
        if not self._slope_ok():
            score *= 0.3
        return self.a_min + (self.a_max - self.a_min) * score

    # ---- portfolio ----
    def portfolio(self, price):
        return self.cash + self.coin * price

    def equity(self, price):
        return self.portfolio(price)

    def inv_ratio(self, price):
        p = self.portfolio(price)
        return (self.coin * price) / p if p > 0 else 0.0

    def _update_spread(self):
        vol = self._volatility()
        target = vol * self.vol_mult
        lo = self.base_spread * self.spread_floor_mult
        hi = self.base_spread * self.spread_ceil_mult
        target = max(lo, min(hi, target))
        self.spread = self.spread * 0.7 + target * 0.3

    # ---- grid ----
    def _place_grid(self, price):
        # keep counter orders, drop stale initial orders
        self.orders = [o for o in self.orders if o.get("is_counter")]
        port = self.portfolio(price)
        coin_val = self.coin * price
        target_val = self.a_base * port
        s = self.spread

        # capital available for grid buys: bring us up toward target + half span
        buy_room_val = max(0.0, (target_val + self.grid_span * port * 0.5) - coin_val)
        buy_room_val = min(buy_room_val, self.cash)
        # inventory available for grid sells: trim toward target - half span
        sell_floor_val = max(0.0, target_val - self.grid_span * port * 0.5)
        sell_room_val = max(0.0, coin_val - sell_floor_val)
        sell_room_val = min(sell_room_val, coin_val)

        # geometric weights (more size on closer levels for fast cycling)
        weights = [ (0.7) ** (i-1) for i in range(1, self.levels + 1)]
        wsum = sum(weights)

        for i in range(1, self.levels + 1):
            w = weights[i-1] / wsum
            bp = price * (1 - s) ** i
            bval = buy_room_val * w
            bsize = bval / bp
            if bval > 1e-6:
                self.orders.append({"side": "buy", "price": bp, "size": bsize,
                                    "is_counter": False, "entry": None})
            sp = price * (1 + s) ** i
            sval = sell_room_val * w
            ssize = sval / sp
            if sval > 1e-6:
                self.orders.append({"side": "sell", "price": sp, "size": ssize,
                                    "is_counter": False, "entry": None})

    def _rebalance_core(self, price):
        """Market rebalance toward a_base when deviation exceeds band."""
        port = self.portfolio(price)
        cur = self.inv_ratio(price)
        if abs(cur - self.a_base) <= self.rebalance_band:
            return
        target_val = self.a_base * port
        delta = target_val - self.coin * price
        if delta > 0:
            buy_val = min(delta, self.cash)
            if buy_val > 1e-6:
                fee = buy_val * self.taker_fee
                self.coin += (buy_val - fee) / price
                self.cash -= buy_val
                self.total_trades += 1
        else:
            sell_val = min(-delta, self.coin * price)
            if sell_val > 1e-6:
                fee = sell_val * self.taker_fee
                self.coin -= sell_val / price
                self.cash += sell_val - fee
                self.total_trades += 1

    def _fill_orders(self, h, l, c):
        filled = [o for o in self.orders
                  if (o["side"] == "buy" and l <= o["price"])
                  or (o["side"] == "sell" and h >= o["price"])]
        for o in filled:
            self.orders.remove(o)
            price = o["price"]
            size = o["size"]
            side = o["side"]
            if side == "buy":
                cost = price * size
                if cost > self.cash:
                    size = self.cash / price if price > 0 else 0
                    cost = price * size
                if size <= 0:
                    continue
                fee = cost * self.maker_fee
                self.cash -= cost + fee
                self.coin += size
                self.total_trades += 1
                # counter sell to lock scalp
                cp = price * (1 + self.spread)
                self.orders.append({"side": "sell", "price": cp, "size": size,
                                    "is_counter": True, "entry": price})
                if o.get("is_counter") and o.get("entry"):
                    self.total_profit += (o["entry"] - price) * size
                    self.cycles += 1
            else:  # sell
                if size > self.coin:
                    size = self.coin
                if size <= 0:
                    continue
                proceeds = price * size
                fee = proceeds * self.maker_fee
                self.coin -= size
                self.cash += proceeds - fee
                self.total_trades += 1
                if o.get("is_counter") and o.get("entry"):
                    self.total_profit += (price - o["entry"]) * size
                    self.cycles += 1
                # counter buy only if we still want inventory (a_base>0)
                if self.a_base > 0.02:
                    cp = price * (1 - self.spread)
                    self.orders.append({"side": "buy", "price": cp, "size": size,
                                        "is_counter": True, "entry": price})

    # ---- main ----
    def process_candle(self, row):
        ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        self._n += 1
        # 1) fill resting orders placed at end of previous bar (uses this bar range)
        if self.closes:
            self._fill_orders(h, l, c)
        # 2) update indicators from closes up to now
        self.closes.append(c)
        self._update_emas()
        self._update_spread()
        self.a_base = self._target_alloc()
        if self.base_price is None:
            self.base_price = c
        # warmup
        if self._es is None:
            return
        # 3) periodic core rebalance (market) toward base allocation
        if self.rebalance_every > 0 and self._n % self.rebalance_every == 0:
            self._rebalance_core(c)
        # 4) refresh grid around current price
        if self.grid_refresh > 0 and self._n % self.grid_refresh == 0:
            self._place_grid(c)
            self.base_price = c


if __name__ == "__main__":
    import data_cache as dc
    from runner import metrics_from_equity
    WINDOWS = [90, 180, 365, 800]
    arr = dc.load("binance", "BTC/USDT", "1h")
    end = arr[-1, 0]

    def run(label, **kw):
        rois = []
        line = []
        for days in WINDOWS:
            cutoff = end - days * 86400 * 1000
            sub = arr[arr[:, 0] >= cutoff]
            tg = TrendGrid(capital=1000.0, **kw)
            for r in sub.tolist():
                tg.process_candle(r)
            eq = []
            # recompute equity curve by re-running (store instead)
            rois.append(None)
        # simpler: single pass storing equity
        out = {}
        for days in WINDOWS:
            cutoff = end - days * 86400 * 1000
            sub = arr[arr[:, 0] >= cutoff]
            tg = TrendGrid(capital=1000.0, **kw)
            eqc = []
            for r in sub.tolist():
                tg.process_candle(r)
                eqc.append(tg.equity(r[4]))
            m = metrics_from_equity(sub[:, 0], eqc, 1000.0, days, sub[0, 1], sub[-1, 4])
            m["trades"] = tg.total_trades
            out[days] = m
        rois = [out[d]["roi"] for d in WINDOWS]
        print(f"\n=== {label} ===")
        for d in WINDOWS:
            m = out[d]
            print(f"  {d:4d}j: ROI={m['roi']:+8.2f}%  maxDD={m['max_dd']:5.1f}%  "
                  f"Sharpe={m['sharpe']:+5.2f}  Calmar={m['calmar']:+6.2f}  "
                  f"B&H={m['bh']:+7.1f}%  vsHold={m['vs_hold']:+7.1f}%  trades={m['trades']}")
        print(f"  >>> worst={min(rois):+.2f}%  mean={sum(rois)/len(rois):+.2f}%  "
              f"all_positive={all(r>0 for r in rois)}")

    run("TrendGrid default (ema100/400, amax0.85)")
    run("TrendGrid amax0.7 gain20", a_max=0.7, alloc_gain=20)
    run("TrendGrid amax0.6 gain30 span0.5", a_max=0.6, alloc_gain=30, grid_span=0.5)
    run("TrendGrid fast ema50/200 amax0.8", ema_fast=50, ema_slow=200, regime_sma=200, a_max=0.8)
