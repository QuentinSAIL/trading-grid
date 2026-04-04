#!/usr/bin/env python3
"""
Grid Bot — Backtester
Simule la strategie grid trading sur des donnees historiques OHLCV.

Usage:
    python backtest.py 30                # 30 derniers jours
    python backtest.py 7 --symbol ETH/USDT
    python backtest.py 90 --spread 0.008 --levels 12
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

import ccxt
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import Progress

load_dotenv()

console = Console()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backtest grid trading strategy")
    parser.add_argument("days", type=int,
                        help="Nombre de jours a backtester")
    parser.add_argument("--symbol",
                        default=os.getenv("SYMBOL", "BTC/USDT"))
    parser.add_argument("--exchange",
                        default=os.getenv("EXCHANGE", "mexc"))
    parser.add_argument("--capital", type=float, default=80,
                        help="Capital simule en USDT (defaut: 80)")
    parser.add_argument("--levels", type=int,
                        default=int(os.getenv("GRID_LEVELS", 10)))
    parser.add_argument("--spread", type=float,
                        default=float(os.getenv("GRID_SPREAD", 0.005)))
    parser.add_argument("--range-pct", type=float,
                        default=float(os.getenv("PRICE_RANGE_PCT", 0.03)))
    parser.add_argument("--stop-loss", type=float,
                        default=float(os.getenv("STOP_LOSS_PCT", 0.50)))
    parser.add_argument("--maker-fee", type=float,
                        default=float(os.getenv("MAKER_FEE", 0.0)),
                        help="Frais maker (defaut: 0%% MEXC)")
    parser.add_argument("--taker-fee", type=float,
                        default=float(os.getenv("TAKER_FEE", 0.001)),
                        help="Frais taker (defaut: 0.1%% MEXC)")
    parser.add_argument("--timeframe", default="1h",
                        help="Timeframe des bougies (defaut: 1h)")
    parser.add_argument("--grid-type", default="geometric",
                        choices=["linear", "geometric"],
                        help="Type de grille (defaut: geometric)")
    parser.add_argument("--weight-factor", type=float, default=1.5,
                        help="Ponderation aux extremes: "
                             "0=egal, 1=double, 2=triple (defaut: 1.5)")
    parser.add_argument("--rsi-period", type=int, default=14,
                        help="Periode RSI (defaut: 14)")
    parser.add_argument("--rsi-strength", type=float, default=1.0,
                        help="Force du signal RSI: "
                             "0=off, 1=normal, 2=agressif (defaut: 1.0)")
    parser.add_argument("--ema-fast", type=int,
                        default=int(os.getenv("EMA_FAST", 12)))
    parser.add_argument("--ema-slow", type=int,
                        default=int(os.getenv("EMA_SLOW", 26)))
    parser.add_argument("--ema-strength", type=float,
                        default=float(os.getenv("EMA_STRENGTH", 0.0)),
                        help="Force du filtre EMA trend: 0=off (defaut: 0)")
    parser.add_argument("--bb-period", type=int,
                        default=int(os.getenv("BB_PERIOD", 20)),
                        help="Periode Bollinger Bands (defaut: 20)")
    parser.add_argument("--bb-mult", type=float,
                        default=float(os.getenv("BB_MULT", 2.0)),
                        help="Multiplicateur BB (defaut: 2.0)")
    bb_default = os.getenv("BB_SPREAD_ADAPT", "false").lower() == "true"
    parser.add_argument("--bb-spread", action="store_true",
                        default=bb_default,
                        help="Adapter le spread aux Bollinger Bands")
    parser.add_argument("--no-bb-spread", action="store_true",
                        default=False,
                        help="Desactiver adaptation BB spread")
    parser.add_argument("--stale-hours", type=int,
                        default=int(os.getenv("STALE_HOURS", 0)),
                        help="Heures avant decay des contre-ordres (0=off)")
    parser.add_argument("--decay-per-hour", type=float,
                        default=float(os.getenv("DECAY_PER_HOUR", 0.0002)),
                        help="Reduction prix/h apres stale (defaut: 0.02%%)")
    parser.add_argument("--trend-spread-mult", type=float,
                        default=float(os.getenv("TREND_SPREAD_MULT", 0.0)),
                        help="Elargir spread en tendance (0=off)")
    parser.add_argument("--dd-threshold", type=float,
                        default=float(os.getenv("DD_THRESHOLD", 1.0)),
                        help="Seuil drawdown pour reduire tailles (1.0=off)")
    parser.add_argument("--dd-factor", type=float,
                        default=float(os.getenv("DD_FACTOR", 0.5)),
                        help="Facteur reduction si DD > seuil")
    parser.add_argument("--max-inv-ratio", type=float,
                        default=float(os.getenv("MAX_INV_RATIO", 1.0)),
                        help="Cap inventaire BTC (1.0=off)")
    parser.add_argument("--initial-btc-pct", type=float,
                        default=float(os.getenv("INITIAL_BTC_PCT", 0.5)),
                        help="Part initiale en BTC (0.5=50/50)")
    parser.add_argument("--trend-liquidation", type=float,
                        default=float(os.getenv("TREND_LIQUIDATION", 0.0)),
                        help="Seuil trend pour liquider BTC (0=off)")
    parser.add_argument("--rebalance-every", type=int,
                        default=int(os.getenv("REBALANCE_EVERY", 6)),
                        help="Force rebalance inventory every N candles (0=off)")
    parser.add_argument("--grid-refresh", type=int,
                        default=int(os.getenv("GRID_REFRESH", 0)),
                        help="Force grid refresh every N candles (0=use rebalance-every)")
    parser.add_argument("--inv-target", type=float,
                        default=float(os.getenv("INV_TARGET", 0.3)),
                        help="Target inventory ratio (default: 0.3)")
    parser.add_argument("--inv-tolerance", type=float,
                        default=float(os.getenv("INV_TOLERANCE", 0.10)),
                        help="Rebalance if inv deviates more than this from target")
    parser.add_argument("--bear-threshold", type=float,
                        default=float(os.getenv("BEAR_THRESHOLD", -0.005)),
                        help="Trend threshold for bear regime (default: -0.005)")
    parser.add_argument("--bear-spread-mult", type=float,
                        default=float(os.getenv("BEAR_SPREAD_MULT", 0.4)),
                        help="Spread multiplier in bear regime (default: 0.4)")
    return parser.parse_args()


def fetch_candles(exchange, symbol: str, timeframe: str,
                  days: int) -> list:
    """Recupere les bougies historiques avec pagination."""
    now = datetime.now(timezone.utc)
    since = int((now - timedelta(days=days)).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    tf_ms = exchange.parse_timeframe(timeframe) * 1000

    all_candles = []
    with Progress(console=console) as progress:
        task = progress.add_task(
            f"[cyan]Telechargement {symbol} ({days}j)...", total=None)
        while since < end:
            try:
                candles = exchange.fetch_ohlcv(
                    symbol, timeframe, since, 1000)
                if not candles:
                    break
                all_candles.extend(candles)
                progress.update(
                    task,
                    description=f"[cyan]{len(all_candles)} bougies...")
                since = candles[-1][0] + tf_ms
                if candles[-1][0] >= end:
                    break
            except ccxt.NetworkError:
                time.sleep(2)
            except ccxt.ExchangeError as e:
                console.print(f"[red]Erreur exchange: {e}[/]")
                break

    all_candles = [c for c in all_candles if c[0] < end]
    return all_candles


class GridBacktester:
    def __init__(self, capital, levels, spread, range_pct,
                 stop_loss_pct, maker_fee, taker_fee,
                 grid_type="linear", weight_factor=0.0,
                 rsi_period=14, rsi_strength=1.0,
                 ema_fast=12, ema_slow=26, ema_strength=0.0,
                 bb_period=20, bb_mult=2.0, bb_spread_adapt=False,
                 stale_hours=0, decay_per_hour=0.0002,
                 trend_spread_mult=0.0, dd_threshold=1.0,
                 dd_factor=0.5, max_inv_ratio=1.0,
                 initial_btc_pct=0.5, trend_liquidation=0.0,
                 rebalance_every=0, grid_refresh=0,
                 inv_target=0.3, inv_tolerance=0.10,
                 bear_threshold=-0.005, bear_spread_mult=0.4):
        self.initial_capital = capital
        self.capital = capital    # USDT disponible
        self.levels = levels
        self.base_spread = spread
        self.spread = spread      # spread courant (dynamique)
        self.range_pct = range_pct
        self.stop_loss_pct = stop_loss_pct
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.grid_type = grid_type
        self.weight_factor = weight_factor
        self.rsi_period = rsi_period
        self.rsi_strength = rsi_strength
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.ema_strength = ema_strength   # 0=off, 1=normal, 2=agressif
        self.bb_period = bb_period
        self.bb_mult = bb_mult
        self.bb_spread_adapt = bb_spread_adapt  # adapter le spread a la BB width

        # Nouvelles features
        self.stale_hours = stale_hours        # heures avant decay (0=off)
        self.decay_per_hour = decay_per_hour  # reduction prix/h apres stale
        self.trend_spread_mult = trend_spread_mult  # elargir spread en tendance
        self.dd_threshold = dd_threshold      # seuil DD pour reduire tailles
        self.dd_factor = dd_factor            # facteur reduction si DD > seuil
        self.max_inv_ratio = max_inv_ratio    # cap inventaire BTC (0-1)
        self.initial_btc_pct = initial_btc_pct  # % capital initial en BTC
        self.trend_liquidation = trend_liquidation  # seuil trend pour liquider
        self.rebalance_every = rebalance_every  # rebal inventaire tous les N candles (0=off)
        self.grid_refresh = grid_refresh if grid_refresh > 0 else rebalance_every
        self.bear_threshold = bear_threshold
        self.bear_spread_mult = bear_spread_mult
        self.inv_target = inv_target            # ratio cible d'inventaire token
        self.inv_tolerance = inv_tolerance      # seuil de deviation pour rebalance

        self.grid_orders = {}
        self._candle_count = 0
        self.btc_held = 0.0
        self.total_profit = 0.0
        self.total_fees = 0.0
        self.total_trades = 0
        self.cycles_completed = 0
        # Swing trading overlay
        self._swing_position = 0.0   # Amount held for swing
        self._swing_entry_price = 0.0
        self._swing_active = False
        self.swing_profits = 0.0
        self.grid_base_price = None
        self.fills = []
        self.equity_curve = []
        self.max_equity = 0.0
        self.max_drawdown = 0.0
        self.stopped = False
        self._oid = 0
        self.stale_decays = 0  # compteur ordres decayed

        # Volatilite dynamique — FLOOR = base_spread
        self.closes_history = []
        self.volatility_window = 24
        self.max_spread = spread * 4.0

        # RSI (Wilder smoothing)
        self.current_rsi = 50.0
        self._rsi_avg_gain = 0.0
        self._rsi_avg_loss = 0.0
        self._rsi_initialized = False

        # EMA (main trend)
        self._ema_fast = 0.0
        self._ema_slow = 0.0
        self._ema_initialized = False
        self.trend = 0.0  # >0 = bullish, <0 = bearish

        # Fast EMA (3/8) for quick inventory decisions
        self._fast_ema3 = 0.0
        self._fast_ema8 = 0.0
        self._fast_ema_initialized = False
        self.fast_trend = 0.0

        # Bollinger Bands
        self.bb_width = 0.0  # % width

        # Stats
        self.rebalance_count = 0
        self._needs_initial_split = True

    def _new_id(self) -> str:
        self._oid += 1
        return f"bt_{self._oid}"

    def _calculate_volatility(self) -> float:
        """Volatilite horaire sur les dernieres closes."""
        closes = self.closes_history[-self.volatility_window:]
        if len(closes) < 2:
            return 0.0
        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                   for i in range(1, len(closes))]
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)

    def _update_dynamic_spread(self):
        """Ajuste le spread selon la volatilite recente.
        PLANCHER = base_spread (jamais en dessous)."""
        if len(self.closes_history) < 3:
            return
        vol = self._calculate_volatility()
        # Spread cible = 2.5x la volatilite horaire
        target = vol * 2.5
        # FLOOR = base_spread, CEIL = max_spread
        target = max(self.base_spread, min(self.max_spread, target))
        # Lissage pour eviter les changements brusques
        self.spread = self.spread * 0.7 + target * 0.3

    def _update_rsi(self):
        """RSI incrementale (Wilder smoothing) — O(1) par bougie."""
        closes = self.closes_history
        n = self.rsi_period
        if len(closes) < 2:
            return
        delta = closes[-1] - closes[-2]
        gain = max(0, delta)
        loss = max(0, -delta)

        if not self._rsi_initialized:
            # Besoin de `period + 1` closes pour initialiser
            if len(closes) < n + 1:
                return
            # Premiere moyenne: SMA sur les `period` premiers deltas
            gains = []
            losses = []
            for i in range(len(closes) - n, len(closes)):
                d = closes[i] - closes[i - 1]
                gains.append(max(0, d))
                losses.append(max(0, -d))
            self._rsi_avg_gain = sum(gains) / n
            self._rsi_avg_loss = sum(losses) / n
            self._rsi_initialized = True
        else:
            # Wilder smoothing: EMA-like avec period
            self._rsi_avg_gain = (self._rsi_avg_gain * (n - 1) + gain) / n
            self._rsi_avg_loss = (self._rsi_avg_loss * (n - 1) + loss) / n

        if self._rsi_avg_loss == 0:
            self.current_rsi = 100.0
        else:
            rs = self._rsi_avg_gain / self._rsi_avg_loss
            self.current_rsi = 100 - (100 / (1 + rs))

    def _update_ema(self):
        """EMA rapide/lente pour detecter la tendance."""
        c = self.closes_history[-1]
        if not self._ema_initialized:
            if len(self.closes_history) >= self.ema_slow_period:
                self._ema_fast = sum(self.closes_history[-self.ema_fast_period:]) / self.ema_fast_period
                self._ema_slow = sum(self.closes_history[-self.ema_slow_period:]) / self.ema_slow_period
                self._ema_initialized = True
            return
        k_fast = 2 / (self.ema_fast_period + 1)
        k_slow = 2 / (self.ema_slow_period + 1)
        self._ema_fast = c * k_fast + self._ema_fast * (1 - k_fast)
        self._ema_slow = c * k_slow + self._ema_slow * (1 - k_slow)
        # Trend: normalise entre -1 et +1
        if self._ema_slow > 0:
            self.trend = (self._ema_fast - self._ema_slow) / self._ema_slow

        # Fast EMA (3/8) for quick inventory decisions
        if not self._fast_ema_initialized:
            if len(self.closes_history) >= 8:
                self._fast_ema3 = sum(self.closes_history[-3:]) / 3
                self._fast_ema8 = sum(self.closes_history[-8:]) / 8
                self._fast_ema_initialized = True
        else:
            k3 = 2 / (3 + 1)
            k8 = 2 / (8 + 1)
            self._fast_ema3 = c * k3 + self._fast_ema3 * (1 - k3)
            self._fast_ema8 = c * k8 + self._fast_ema8 * (1 - k8)
            if self._fast_ema8 > 0:
                self.fast_trend = (self._fast_ema3 - self._fast_ema8) / self._fast_ema8

    def _update_bb(self):
        """Bollinger Bands width — mesure de compression/expansion."""
        if len(self.closes_history) < self.bb_period:
            return
        window = self.closes_history[-self.bb_period:]
        sma = sum(window) / self.bb_period
        if sma == 0:
            return
        variance = sum((x - sma) ** 2 for x in window) / self.bb_period
        std = math.sqrt(variance)
        upper = sma + self.bb_mult * std
        lower = sma - self.bb_mult * std
        self.bb_width = (upper - lower) / sma  # en %

    def _effective_spread(self) -> float:
        """Spread ajuste selon la tendance: plus large en tendance forte."""
        s = self.spread
        if self.trend_spread_mult > 0 and self._ema_initialized:
            t = max(-0.05, min(0.05, self.trend))
            normalized = abs(t) / 0.05  # 0 a 1
            s *= (1.0 + normalized * self.trend_spread_mult)
        return min(s, self.max_spread)

    def _dd_multiplier(self) -> float:
        """Reduit les tailles quand le drawdown depasse le seuil."""
        if self.dd_threshold >= 1.0 or self.max_equity <= 0:
            return 1.0
        current_equity = self.capital + self.btc_held * (
            self.closes_history[-1] if self.closes_history else 0)
        current_dd = (self.max_equity - current_equity) / self.max_equity
        if current_dd > self.dd_threshold:
            return self.dd_factor
        return 1.0

    def _ema_multiplier(self, side: str) -> float:
        """Multiplicateur EMA: favorise les ordres dans le sens de la tendance.
        Trend haussiere: boost les buys, reduit les sells.
        Trend baissiere: boost les sells, reduit les buys."""
        if self.ema_strength == 0 or not self._ema_initialized:
            return 1.0
        s = self.ema_strength
        # trend est typiquement entre -0.05 et +0.05
        t = max(-0.05, min(0.05, self.trend))
        normalized = t / 0.05  # -1 a +1

        if side == "buy":
            # Trend up -> boost buys (max 1+s), trend down -> reduce (min 1-s*0.5)
            return max(0.2, 1.0 + normalized * s * 0.5)
        else:
            # Trend up -> reduce sells, trend down -> boost sells
            return max(0.2, 1.0 - normalized * s * 0.5)

    def _rsi_multiplier(self, side: str) -> float:
        """Multiplicateur RSI pour la taille des ordres.
        RSI < 30 (survendu): boost les buys, reduit les sells
        RSI > 70 (surachete): boost les sells, reduit les buys
        RSI 30-70 (neutre): pas d'effet"""
        if self.rsi_strength == 0:
            return 1.0
        rsi = self.current_rsi
        s = self.rsi_strength

        if side == "buy":
            if rsi < 30:
                # Survendu: acheter plus (RSI 0->30 donne 1+s -> 1)
                return 1.0 + s * (30 - rsi) / 30
            elif rsi > 70:
                # Surachete: acheter moins (RSI 70->100 donne 1 -> max(0.1, 1-s))
                return max(0.1, 1.0 - s * (rsi - 70) / 30)
        else:  # sell
            if rsi > 70:
                # Surachete: vendre plus
                return 1.0 + s * (rsi - 70) / 30
            elif rsi < 30:
                # Survendu: vendre moins
                return max(0.1, 1.0 - s * (30 - rsi) / 30)
        return 1.0

    def _inventory_ratio(self, price: float) -> float:
        """Part du portefeuille en BTC (0 = tout USDT, 1 = tout BTC)."""
        portfolio = self.capital + self.btc_held * price
        if portfolio <= 0:
            return 0.5
        return max(0.0, (self.btc_held * price) / portfolio)

    def _grid_price(self, base_price: float, i: int, side: str) -> float:
        """Prix de la grille au niveau i selon le type."""
        if self.grid_type == "geometric":
            if side == "buy":
                return base_price * (1 - self.spread) ** i
            else:
                return base_price * (1 + self.spread) ** i
        else:  # linear
            if side == "buy":
                return base_price * (1 - i * self.spread)
            else:
                return base_price * (1 + i * self.spread)

    def _weighted_multiplier(self, i: int) -> float:
        """Multiplicateur de taille au niveau i.
        i=1 (proche du prix) -> 1x, i=levels (loin) -> (1+weight_factor)x
        Plus weight_factor est eleve, plus les ordres loin du prix sont gros.
        = DCA agressif sur les buys, prise de profit agressive sur les sells."""
        if self.levels <= 1 or self.weight_factor == 0:
            return 1.0
        progress = (i - 1) / (self.levels - 1)  # 0 a 1
        return 1.0 + self.weight_factor * progress

    def order_size(self, base_price: float, side: str,
                   level: int = 1) -> float:
        """Taille par niveau: ponderation + inventaire + RSI + DD protection."""
        portfolio_value = self.capital + self.btc_held * base_price
        effective = portfolio_value * 0.9

        total_weight = sum(self._weighted_multiplier(i)
                           for i in range(1, self.levels + 1))
        base_size_usdt = (effective / 2) / total_weight

        # Poids du niveau (DCA)
        base_size_usdt *= self._weighted_multiplier(level)

        # RSI: boost buys en survendu, boost sells en surachete
        base_size_usdt *= self._rsi_multiplier(side)

        # EMA: favoriser les ordres dans le sens de la tendance
        base_size_usdt *= self._ema_multiplier(side)

        # Drawdown protection: reduire les tailles si DD > seuil
        base_size_usdt *= self._dd_multiplier()

        # Inventaire: eviter l'accumulation excessive
        inv_ratio = self._inventory_ratio(base_price)

        # Cap inventaire: bloquer les buys si trop de BTC
        if side == "buy" and inv_ratio >= self.max_inv_ratio:
            return 0.0

        if side == "buy":
            if inv_ratio > 0.6:
                factor = max(0.1, 1.0 - (inv_ratio - 0.5) * 2)
                base_size_usdt *= factor
        else:
            if inv_ratio > 0.6:
                factor = min(2.0, 1.0 + (inv_ratio - 0.5) * 2)
                base_size_usdt *= factor
            elif inv_ratio < 0.3:
                factor = max(0.2, inv_ratio / 0.5)
                base_size_usdt *= factor

        return base_size_usdt / base_price

    def place_grid(self, price: float, timestamp: int):
        """Place la grille avec spread dynamique, spacing geometrique/lineaire,
        sizing pondere et inventaire-aware.
        Preserve les contre-ordres existants.
        TREND-AWARE: adapte les parametres au regime de marche."""
        counter_orders = {oid: o for oid, o in self.grid_orders.items()
                          if o.get("is_counter")}
        self.grid_orders = dict(counter_orders)
        self.grid_base_price = price
        old_spread = self.spread
        self.spread = self._effective_spread()

        quote_used = 0.0
        base_used = 0.0

        # Adaptive grid: adjust spread and levels based on regime
        active_levels = self.levels
        active_spread = self.spread
        if self._ema_initialized and self.trend < -0.005:
            # Bear regime: tighter spread for faster cycles on bounces
            active_spread = max(self.base_spread * 0.3, self.spread * 0.4)
            active_levels = max(1, self.levels + 1)  # one more level for coverage

        # Trend-aware buy scaling
        buy_scale = 1.0
        if self._ema_initialized and self.trend < -0.005:
            buy_scale = max(0.1, 1.0 + self.trend * 20)

        # Temporarily set spread for grid price calculation
        saved_spread = self.spread
        self.spread = active_spread

        for i in range(1, active_levels + 1):
            buy_size = self.order_size(price, "buy", i) * buy_scale
            buy_price = self._grid_price(price, i, "buy")
            buy_cost = buy_size * buy_price
            if buy_cost > 0 and quote_used + buy_cost <= self.capital:
                buy_id = self._new_id()
                self.grid_orders[buy_id] = {
                    "side": "buy",
                    "price": buy_price,
                    "size": buy_size,
                    "is_counter": False,
                }
                quote_used += buy_cost

            sell_size = self.order_size(price, "sell", i)
            sell_price = self._grid_price(price, i, "sell")
            if sell_size > 0 and base_used + sell_size <= self.btc_held:
                sell_id = self._new_id()
                self.grid_orders[sell_id] = {
                    "side": "sell",
                    "price": sell_price,
                    "size": sell_size,
                    "is_counter": False,
                }
                base_used += sell_size

        self.spread = saved_spread  # Restore original spread


    def process_candle(self, candle: list):
        """Simule une bougie: [timestamp, open, high, low, close, volume]."""
        ts, o, h, l, c, v = candle
        if self.stopped:
            return

        # Tracking indicateurs
        self.closes_history.append(c)
        self._update_dynamic_spread()
        self._update_rsi()
        self._update_ema()
        self._update_bb()

        # BB spread adaptation: utilise la BB width comme spread dynamique
        if self.bb_spread_adapt and self.bb_width > 0:
            bb_spread = self.bb_width / (2 * self.levels)
            bb_spread = max(self.base_spread, min(self.max_spread, bb_spread))
            self.spread = self.spread * 0.7 + bb_spread * 0.3

        # Demarrage: acheter du BTC selon le ratio initial
        if self._needs_initial_split:
            btc_to_buy = (self.capital * self.initial_btc_pct) / o
            self.capital -= btc_to_buy * o
            self.btc_held += btc_to_buy
            self._needs_initial_split = False

        if self.grid_base_price is None:
            self.place_grid(o, ts)

        # Stale order decay: baisser le prix des contre-ordres ages
        if self.stale_hours > 0:
            stale_threshold_ms = self.stale_hours * 3600 * 1000
            for oid, order in list(self.grid_orders.items()):
                if not order.get("is_counter"):
                    continue
                placed = order.get("placed_at", 0)
                if placed <= 0:
                    continue
                age_ms = ts - placed
                if age_ms <= stale_threshold_ms:
                    continue
                # Heures depuis que l'ordre est stale
                stale_h = (age_ms - stale_threshold_ms) / 3_600_000
                orig_price = order.get("original_fill_price", 0)
                if orig_price <= 0:
                    continue
                if order["side"] == "sell":
                    # Baisser le prix de vente vers le break-even
                    floor = orig_price * (1 + self.maker_fee * 2 + 0.0001)
                    decay = self.decay_per_hour * stale_h * orig_price
                    new_price = max(floor, order["price"] - decay)
                    if new_price < order["price"]:
                        order["price"] = new_price
                        self.stale_decays += 1
                else:  # buy counter
                    # Monter le prix d'achat vers le break-even
                    ceil = orig_price * (1 - self.maker_fee * 2 - 0.0001)
                    decay = self.decay_per_hour * stale_h * orig_price
                    new_price = min(ceil, order["price"] + decay)
                    if new_price > order["price"]:
                        order["price"] = new_price
                        self.stale_decays += 1

        # Verifier les fills
        filled_ids = []
        for oid, order in self.grid_orders.items():
            if order["side"] == "buy" and l <= order["price"]:
                filled_ids.append(oid)
            elif order["side"] == "sell" and h >= order["price"]:
                filled_ids.append(oid)

        for oid in filled_ids:
            order = self.grid_orders.pop(oid)
            fill_price = order["price"]
            size = order["size"]
            side = order["side"]
            is_counter = order["is_counter"]

            # Protection: ne jamais vendre plus de BTC qu'on en a
            if side == "sell" and size > self.btc_held:
                if self.btc_held <= 0:
                    continue  # skip ce fill
                size = self.btc_held  # vendre ce qu'on a

            # Protection: ne jamais acheter plus qu'on ne peut payer
            cost = fill_price * size
            if side == "buy" and cost > self.capital:
                if self.capital <= 0:
                    continue
                size = self.capital / fill_price

            fee = fill_price * size * self.maker_fee
            self.total_fees += fee

            if side == "buy":
                self.capital -= fill_price * size
                self.btc_held += size
            else:
                self.btc_held -= size
                self.capital += fill_price * size

            profit = -fee
            if is_counter:
                original_price = order.get("original_fill_price", 0)
                if original_price:
                    if side == "sell":
                        gross = (fill_price - original_price) * size
                    else:
                        gross = (original_price - fill_price) * size
                else:
                    gross = fill_price * self.spread * size
                profit = gross - 2 * fee
                self.cycles_completed += 1
            self.total_profit += profit

            self.total_trades += 1
            self.fills.append({
                "time": ts, "side": side, "price": fill_price,
                "profit": profit, "is_counter": is_counter,
            })

            # Contre-ordre avec le spread effectif (tendance-adaptatif)
            # Scalp orders utilisent leur propre spread serré
            if "scalp_spread" in order:
                eff_spread = order["scalp_spread"]
            else:
                eff_spread = self._effective_spread()
            inv = self._inventory_ratio(c if c else fill_price)
            if side == "buy":
                counter_price = fill_price * (1 + eff_spread)
                counter_size = size
                # Si trop de token, reduire le contre-achat futur
                # (le sell va quand meme se faire, mais on marque pour le prochain cycle)
                new_id = self._new_id()
                self.grid_orders[new_id] = {
                    "side": "sell", "price": counter_price,
                    "size": counter_size, "is_counter": True,
                    "original_fill_price": fill_price,
                    "placed_at": ts,
                }
            else:
                counter_price = fill_price * (1 - eff_spread)
                counter_size = size
                # Si trop de token deja, reduire le contre-achat
                if inv > self.inv_target + self.inv_tolerance:
                    scale = max(0.2, 1.0 - (inv - self.inv_target) * 3)
                    counter_size *= scale
                new_id = self._new_id()
                self.grid_orders[new_id] = {
                    "side": "buy", "price": counter_price,
                    "size": counter_size, "is_counter": True,
                    "original_fill_price": fill_price,
                    "placed_at": ts,
                }

        # Equity = valeur totale du portefeuille
        portfolio_value = self.capital + self.btc_held * c
        self.equity_curve.append((ts, portfolio_value))
        if portfolio_value > self.max_equity:
            self.max_equity = portfolio_value
        dd = ((self.max_equity - portfolio_value) / self.max_equity
              if self.max_equity > 0 else 0)
        if dd > self.max_drawdown:
            self.max_drawdown = dd

        # Stop loss base sur la valeur du portefeuille
        loss_pct = ((self.initial_capital - portfolio_value)
                    / self.initial_capital)
        if loss_pct >= self.stop_loss_pct:
            self.stopped = True
            return

        # Trend liquidation: vendre du BTC si tendance tres baissiere
        if (self.trend_liquidation > 0 and self._ema_initialized
                and self.trend < -self.trend_liquidation):
            inv = self._inventory_ratio(c)
            # Si on a plus de 40% en BTC et tendance fortement baissiere
            if inv > 0.4:
                # Vendre 10% du BTC pour reduire l'exposition
                sell_pct = min(0.1, (inv - 0.3))
                sell_amount = self.btc_held * sell_pct
                if sell_amount > 0:
                    proceeds = sell_amount * c
                    fee = proceeds * self.taker_fee
                    self.btc_held -= sell_amount
                    self.capital += proceeds - fee
                    self.total_fees += fee
                    self.total_trades += 1

        # Inventaire DYNAMIQUE: rebalance frequent, grid refresh independant
        self._candle_count += 1

        # 1) Rebalance inventaire
        if (self.rebalance_every > 0
                and self._candle_count % self.rebalance_every == 0):
            dynamic_target = self.inv_target
            if self._ema_initialized:
                # Utiliser fast_trend (EMA 3/8) pour réagir plus vite en bear
                ft = self.fast_trend if self._fast_ema_initialized else self.trend
                # En bear (fast_trend < 0): utiliser fast_trend pour sell rapide
                # En bull (fast_trend > 0): utiliser slow trend pour buy progressif
                t_sell = max(-0.05, min(0.05, ft))
                t_buy = max(-0.05, min(0.05, self.trend))
                t = t_sell if ft < 0 else t_buy
                trend_mult = 1.0 + t * 30
                dynamic_target = max(0.01, min(0.30, self.inv_target * trend_mult))

            inv = self._inventory_ratio(c)
            if inv > dynamic_target + self.inv_tolerance:
                portfolio_val = self.capital + self.btc_held * c
                target_token_val = portfolio_val * dynamic_target
                current_token_val = self.btc_held * c
                excess_val = current_token_val - target_token_val
                if excess_val > 0:
                    sell_amount = excess_val / c * 0.8
                    if sell_amount > 0 and sell_amount <= self.btc_held:
                        proceeds = sell_amount * c
                        fee = proceeds * self.taker_fee
                        self.btc_held -= sell_amount
                        self.capital += proceeds - fee
                        self.total_fees += fee
                        self.total_trades += 1
            elif (inv < dynamic_target - self.inv_tolerance
                  and self._ema_initialized and self.trend > 0.01):
                portfolio_val = self.capital + self.btc_held * c
                target_token_val = portfolio_val * dynamic_target
                current_token_val = self.btc_held * c
                deficit_val = target_token_val - current_token_val
                if deficit_val > 0 and deficit_val < self.capital * 0.3:
                    buy_amount = deficit_val / c * 0.5
                    cost = buy_amount * c
                    fee = cost * self.taker_fee
                    if cost + fee <= self.capital:
                        self.capital -= cost + fee
                        self.btc_held += buy_amount
                        self.total_fees += fee
                        self.total_trades += 1

        # 2) Grid refresh (moins frequent, laisse les ordres travailler)
        if (self.grid_refresh > 0
                and self._candle_count % self.grid_refresh == 0):
            self.place_grid(c, ts)

        # Trailing grid base + recentrage
        if self.grid_base_price:
            drift = abs(c - self.grid_base_price) / self.grid_base_price

            if drift > 0.005:
                self.grid_base_price = (self.grid_base_price * 0.9
                                        + c * 0.1)

            if drift > self.range_pct:
                self.rebalance_count += 1
                self.place_grid(c, ts)


def display_results(bt: GridBacktester, candles: list, args):
    start_price = candles[0][1]
    end_price = candles[-1][4]
    hold_return = (end_price - start_price) / start_price * 100
    start_date = datetime.fromtimestamp(candles[0][0] / 1000, tz=TZ)
    end_date = datetime.fromtimestamp(candles[-1][0] / 1000, tz=TZ)

    final_value = bt.capital + bt.btc_held * end_price
    pnl = final_value - bt.initial_capital
    roi = (pnl / bt.initial_capital * 100) if bt.initial_capital else 0
    daily_roi = roi / args.days if args.days else 0

    # Config panel
    config = Table(show_header=False, expand=True, padding=(0, 1))
    config.add_column("Param", style="dim")
    config.add_column("Valeur", style="bold")
    config.add_row("Paire", args.symbol)
    config.add_row("Periode",
                   f"{start_date:%Y-%m-%d} -> {end_date:%Y-%m-%d} "
                   f"({args.days}j)")
    config.add_row("Capital", f"{bt.initial_capital:.2f} USDT")
    config.add_row("Niveaux",
                   f"{bt.levels} x2 = {bt.levels * 2} ordres")
    config.add_row("Spread",
                   f"{bt.base_spread * 100:.2f}% base → "
                   f"{bt.spread * 100:.2f}% final (dynamique)")
    config.add_row("Grille",
                   f"{bt.grid_type} | poids: {bt.weight_factor}x aux extremes")
    rsi_label = "off" if bt.rsi_strength == 0 else f"{bt.rsi_strength}x (RSI-{bt.rsi_period})"
    config.add_row("RSI", rsi_label)
    ema_label = "off" if bt.ema_strength == 0 else f"{bt.ema_strength}x (EMA {bt.ema_fast_period}/{bt.ema_slow_period})"
    config.add_row("EMA trend", ema_label)
    config.add_row("BB spread", "on" if bt.bb_spread_adapt else "off")
    stale_label = "off" if bt.stale_hours == 0 else f"{bt.stale_hours}h / decay {bt.decay_per_hour*100:.02f}%/h"
    config.add_row("Stale decay", stale_label)
    config.add_row("Trend spread", "off" if bt.trend_spread_mult == 0 else f"{bt.trend_spread_mult}x")
    dd_label = "off" if bt.dd_threshold >= 1.0 else f">{bt.dd_threshold*100:.0f}% -> x{bt.dd_factor}"
    config.add_row("DD protection", dd_label)
    inv_label = "off" if bt.max_inv_ratio >= 1.0 else f"{bt.max_inv_ratio*100:.0f}%"
    config.add_row("Inv cap", inv_label)
    config.add_row("Frais",
                   f"maker {bt.maker_fee * 100:.2f}% / "
                   f"taker {bt.taker_fee * 100:.2f}%")
    config.add_row("Bougies", f"{len(candles)} ({args.timeframe})")

    console.print(Panel(config, title="[bold]Configuration[/bold]",
                        border_style="blue"))

    # Results panel
    results = Table(show_header=False, expand=True, padding=(0, 1))
    results.add_column("Metric", style="dim")
    results.add_column("Valeur")

    pnl_color = "green" if pnl >= 0 else "red"
    results.add_row("Valeur finale",
                    Text(f"{final_value:.2f} USDT", style="bold"))
    results.add_row("PnL portefeuille",
                    Text(f"{pnl:+.4f} USDT", style=f"bold {pnl_color}"))
    results.add_row("Profit grid (cycles)",
                    Text(f"{bt.total_profit:+.4f} USDT",
                         style=f"bold {pnl_color}"))
    results.add_row("ROI",
                    Text(f"{roi:+.2f}%", style=f"bold {pnl_color}"))
    results.add_row("ROI/jour",
                    Text(f"{daily_roi:+.3f}%/j", style=pnl_color))
    results.add_row("ROI projete/mois",
                    Text(f"{daily_roi * 30:+.2f}%/mois", style=pnl_color))
    results.add_row("", "")
    results.add_row("Trades total", f"{bt.total_trades}")
    results.add_row("Cycles completes", f"{bt.cycles_completed}")
    results.add_row("Frais total",
                    Text(f"-{bt.total_fees:.4f} USDT", style="red"))
    results.add_row("BTC restant",
                    Text(f"{bt.btc_held:.6f}", style="cyan"))
    results.add_row("Max drawdown",
                    Text(f"{bt.max_drawdown * 100:.2f}%", style="yellow"))
    results.add_row("Rebalances", f"{bt.rebalance_count}")
    if bt.stale_decays > 0:
        results.add_row("Stale decays", f"{bt.stale_decays}")
    inv_ratio = bt._inventory_ratio(end_price) * 100
    inv_style = "yellow" if inv_ratio > 60 else "green"
    results.add_row("Inventaire BTC",
                    Text(f"{inv_ratio:.1f}% du portefeuille",
                         style=inv_style))
    if bt.stopped:
        results.add_row("Stop loss",
                        Text("DECLENCHE", style="bold red"))
    results.add_row("", "")
    results.add_row("Buy & Hold",
                    Text(f"{hold_return:+.2f}%", style="cyan"))
    grid_vs_hold = roi - hold_return
    cmp_color = "green" if grid_vs_hold >= 0 else "red"
    results.add_row("Grid vs Hold",
                    Text(f"{grid_vs_hold:+.2f}%",
                         style=f"bold {cmp_color}"))

    console.print(Panel(results, title="[bold]Resultats[/bold]",
                        border_style="green"))

    # Equity sparkline
    if bt.equity_curve:
        equities = [e[1] for e in bt.equity_curve]
        min_eq = min(equities)
        max_eq = max(equities)
        width = min(80, console.width - 10)
        step = max(1, len(equities) // width)
        sampled = equities[::step][:width]

        bars = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
        if max_eq > min_eq:
            line = ""
            for val in sampled:
                idx = int((val - min_eq) / (max_eq - min_eq)
                          * (len(bars) - 1))
                color = "green" if val >= bt.initial_capital else "red"
                line += f"[{color}]{bars[idx]}[/{color}]"
        else:
            line = bars[4] * len(sampled)

        equity_text = Text.from_markup(
            f"[dim]{min_eq:.2f}[/] {line} [dim]{max_eq:.2f}[/]"
        )
        console.print(Panel(equity_text,
                            title="[bold]Equity curve[/bold]",
                            border_style="yellow"))

    # Top fills
    profitable_fills = [f for f in bt.fills if f["profit"] > 0]
    if profitable_fills:
        profitable_fills.sort(key=lambda f: f["profit"], reverse=True)
        top = profitable_fills[:5]
        fills_table = Table(show_header=True, header_style="bold",
                            expand=True, padding=(0, 1))
        fills_table.add_column("Date", style="dim")
        fills_table.add_column("Side", justify="center")
        fills_table.add_column("Prix", justify="right")
        fills_table.add_column("Profit", justify="right", style="green")
        for f in top:
            dt = datetime.fromtimestamp(f["time"] / 1000, tz=TZ)
            fills_table.add_row(
                f"{dt:%Y-%m-%d %H:%M}",
                f["side"].upper(),
                f"{f['price']:.2f}",
                f"+{f['profit']:.4f}",
            )
        console.print(Panel(fills_table,
                            title="[bold]Top 5 fills[/bold]",
                            border_style="magenta"))


def main():
    args = parse_args()

    console.print(Panel(
        f"[bold cyan]Grid Backtester[/]\n"
        f"[dim]{args.symbol} | {args.days} jours | "
        f"{args.levels} niveaux | spread {args.spread*100:.2f}%[/]",
        border_style="blue",
    ))

    try:
        exchange_class = getattr(ccxt, args.exchange)
        exchange = exchange_class({"enableRateLimit": True})
        exchange.load_markets()
    except Exception as e:
        console.print(
            f"[bold red]Erreur connexion {args.exchange}: {e}[/]")
        sys.exit(1)

    if args.symbol not in exchange.markets:
        console.print(
            f"[bold red]Paire {args.symbol} non disponible "
            f"sur {args.exchange}[/]")
        sys.exit(1)

    candles = fetch_candles(exchange, args.symbol, args.timeframe, args.days)
    if len(candles) < 10:
        console.print(
            "[bold red]Pas assez de donnees pour backtester[/]")
        sys.exit(1)

    console.print(f"[green]{len(candles)} bougies chargees[/]\n")

    bt = GridBacktester(
        capital=args.capital,
        levels=args.levels,
        spread=args.spread,
        range_pct=args.range_pct,
        stop_loss_pct=args.stop_loss,
        maker_fee=args.maker_fee,
        taker_fee=args.taker_fee,
        grid_type=args.grid_type,
        weight_factor=args.weight_factor,
        rsi_period=args.rsi_period,
        rsi_strength=args.rsi_strength,
        ema_fast=args.ema_fast,
        ema_slow=args.ema_slow,
        ema_strength=args.ema_strength,
        bb_period=args.bb_period,
        bb_mult=args.bb_mult,
        bb_spread_adapt=args.bb_spread and not args.no_bb_spread,
        stale_hours=args.stale_hours,
        decay_per_hour=args.decay_per_hour,
        trend_spread_mult=args.trend_spread_mult,
        dd_threshold=args.dd_threshold,
        dd_factor=args.dd_factor,
        max_inv_ratio=args.max_inv_ratio,
        initial_btc_pct=args.initial_btc_pct,
        trend_liquidation=args.trend_liquidation,
        rebalance_every=args.rebalance_every,
        grid_refresh=args.grid_refresh,
        inv_target=args.inv_target,
        inv_tolerance=args.inv_tolerance,
        bear_threshold=args.bear_threshold,
        bear_spread_mult=args.bear_spread_mult,
    )

    with Progress(console=console) as progress:
        task = progress.add_task("[cyan]Simulation...",
                                 total=len(candles))
        for candle in candles:
            bt.process_candle(candle)
            progress.advance(task)

    console.print()
    display_results(bt, candles, args)


if __name__ == "__main__":
    main()
