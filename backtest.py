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
                        default=float(os.getenv("STOP_LOSS_PCT", 0.25)))
    parser.add_argument("--maker-fee", type=float,
                        default=float(os.getenv("MAKER_FEE", 0.0)),
                        help="Frais maker (defaut: 0%% MEXC)")
    parser.add_argument("--taker-fee", type=float,
                        default=float(os.getenv("TAKER_FEE", 0.001)),
                        help="Frais taker (defaut: 0.1%% MEXC)")
    parser.add_argument("--timeframe", default="1h",
                        help="Timeframe des bougies (defaut: 1h)")
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
                 stop_loss_pct, maker_fee, taker_fee):
        self.initial_capital = capital
        self.capital = capital    # USDT disponible
        self.levels = levels
        self.base_spread = spread
        self.spread = spread      # spread courant (dynamique)
        self.range_pct = range_pct
        self.stop_loss_pct = stop_loss_pct
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee

        self.grid_orders = {}
        self.btc_held = 0.0       # BTC detenu (pour tracking inventaire)
        self.total_profit = 0.0
        self.total_fees = 0.0
        self.total_trades = 0
        self.cycles_completed = 0
        self.grid_base_price = None
        self.fills = []
        self.equity_curve = []
        self.max_equity = 0.0
        self.max_drawdown = 0.0
        self.stopped = False
        self._oid = 0

        # Volatilite dynamique
        self.closes_history = []
        self.volatility_window = 24
        self.min_spread = spread * 0.5
        self.max_spread = spread * 4.0

        # Stats supplementaires
        self.rebalance_count = 0

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
        """Ajuste le spread selon la volatilite recente."""
        if len(self.closes_history) < 3:
            return
        vol = self._calculate_volatility()
        # Spread cible = 2.5x la volatilite horaire (capture le mouvement)
        target = vol * 2.5
        target = max(self.min_spread, min(self.max_spread, target))
        # Lissage pour eviter les changements brusques
        self.spread = self.spread * 0.7 + target * 0.3

    def _inventory_ratio(self, price: float) -> float:
        """Part du portefeuille en BTC (0 = tout USDT, 1 = tout BTC)."""
        portfolio = self.capital + self.btc_held * price
        if portfolio <= 0:
            return 0.5
        return (self.btc_held * price) / portfolio

    def order_size(self, base_price: float, side: str) -> float:
        """Taille par niveau avec gestion d'inventaire.
        Reduit les achats quand trop de BTC, et inversement."""
        portfolio_value = self.capital + self.btc_held * base_price
        effective = portfolio_value * 0.9  # 90% allocation
        base_size_usdt = (effective / 2) / self.levels

        inv_ratio = self._inventory_ratio(base_price)

        if side == "buy":
            # Trop de BTC -> reduire les achats
            if inv_ratio > 0.5:
                factor = max(0.2, 1.0 - (inv_ratio - 0.5) * 3)
                base_size_usdt *= factor
        else:
            # Trop de BTC -> augmenter les ventes
            if inv_ratio > 0.5:
                factor = min(1.8, 1.0 + (inv_ratio - 0.5) * 2)
                base_size_usdt *= factor
            # Pas assez de BTC -> reduire les ventes
            elif inv_ratio < 0.2:
                factor = max(0.3, inv_ratio / 0.4)
                base_size_usdt *= factor

        return base_size_usdt / base_price

    def place_grid(self, price: float, timestamp: int):
        """Place la grille avec spread dynamique et sizing inventaire-aware.
        Preserve les contre-ordres existants."""
        # Sauvegarder les contre-ordres
        counter_orders = {oid: o for oid, o in self.grid_orders.items()
                          if o.get("is_counter")}
        self.grid_orders = dict(counter_orders)
        self.grid_base_price = price

        quote_used = 0.0
        base_used = 0.0

        for i in range(1, self.levels + 1):
            # Buy orders (si assez de USDT cumulativement)
            buy_size = self.order_size(price, "buy")
            buy_price = price * (1 - i * self.spread)
            buy_cost = buy_size * buy_price
            if quote_used + buy_cost <= self.capital:
                buy_id = self._new_id()
                self.grid_orders[buy_id] = {
                    "side": "buy",
                    "price": buy_price,
                    "size": buy_size,
                    "is_counter": False,
                }
                quote_used += buy_cost

            # Sell orders (si assez de BTC cumulativement)
            sell_size = self.order_size(price, "sell")
            sell_price = price * (1 + i * self.spread)
            if base_used + sell_size <= self.btc_held:
                sell_id = self._new_id()
                self.grid_orders[sell_id] = {
                    "side": "sell",
                    "price": sell_price,
                    "size": sell_size,
                    "is_counter": False,
                }
                base_used += sell_size

    def process_candle(self, candle: list):
        """Simule une bougie: [timestamp, open, high, low, close, volume]."""
        ts, o, h, l, c, v = candle
        if self.stopped:
            return

        # Tracking volatilite
        self.closes_history.append(c)
        self._update_dynamic_spread()

        if self.grid_base_price is None:
            self.place_grid(o, ts)

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

            fee = fill_price * size * self.maker_fee
            self.total_fees += fee

            # Mettre a jour l'inventaire
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

            # Contre-ordre avec le spread courant (dynamique)
            if side == "buy":
                counter_price = fill_price * (1 + self.spread)
                new_id = self._new_id()
                self.grid_orders[new_id] = {
                    "side": "sell", "price": counter_price,
                    "size": size, "is_counter": True,
                    "original_fill_price": fill_price,
                }
            else:
                counter_price = fill_price * (1 - self.spread)
                new_id = self._new_id()
                self.grid_orders[new_id] = {
                    "side": "buy", "price": counter_price,
                    "size": size, "is_counter": True,
                    "original_fill_price": fill_price,
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

        # Trailing grid base: glisse progressivement vers le prix actuel
        if self.grid_base_price:
            drift = abs(c - self.grid_base_price) / self.grid_base_price

            # Micro-ajustement continu: deplace la base de 10% vers le prix
            if drift > 0.005:
                self.grid_base_price = (self.grid_base_price * 0.9
                                        + c * 0.1)

            # Recentrage complet si drift trop important
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
