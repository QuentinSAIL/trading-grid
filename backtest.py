#!/usr/bin/env python3
"""
Grid Bot — Backtester
Simule la stratégie grid trading sur des données historiques OHLCV.

Usage:
    python backtest.py 30                # 30 derniers jours
    python backtest.py 7 --symbol ETH/USDT
    python backtest.py 90 --spread 0.008 --levels 12
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta

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
    parser = argparse.ArgumentParser(description="Backtest grid trading strategy")
    parser.add_argument("days", type=int, help="Nombre de jours a backtester")
    parser.add_argument("--symbol", default=os.getenv("SYMBOL", "BTC/USDT"))
    parser.add_argument("--exchange", default=os.getenv("EXCHANGE", "mexc"))
    parser.add_argument("--capital", type=float, default=float(os.getenv("CAPITAL", 80)))
    parser.add_argument("--levels", type=int, default=int(os.getenv("GRID_LEVELS", 10)))
    parser.add_argument("--spread", type=float, default=float(os.getenv("GRID_SPREAD", 0.005)))
    parser.add_argument("--range-pct", type=float, default=float(os.getenv("PRICE_RANGE_PCT", 0.05)))
    parser.add_argument("--stop-loss", type=float, default=float(os.getenv("STOP_LOSS_PCT", 0.08)))
    parser.add_argument("--fee", type=float, default=0.001, help="Frais taker (defaut: 0.1%%)")
    parser.add_argument("--timeframe", default="1h", help="Timeframe des bougies (defaut: 1h)")
    return parser.parse_args()


def fetch_candles(exchange, symbol: str, timeframe: str, days: int) -> list:
    """Recupere les bougies historiques avec pagination."""
    now = datetime.now(timezone.utc)
    since = int((now - timedelta(days=days)).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    tf_ms = exchange.parse_timeframe(timeframe) * 1000

    all_candles = []
    with Progress(console=console) as progress:
        task = progress.add_task(f"[cyan]Telechargement {symbol} ({days}j)...", total=None)
        while since < end:
            try:
                candles = exchange.fetch_ohlcv(symbol, timeframe, since, 1000)
                if not candles:
                    break
                all_candles.extend(candles)
                progress.update(task, description=f"[cyan]{len(all_candles)} bougies...")
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
    def __init__(self, capital, levels, spread, range_pct, stop_loss_pct, fee):
        self.capital = capital
        self.levels = levels
        self.spread = spread
        self.range_pct = range_pct
        self.stop_loss_pct = stop_loss_pct
        self.fee = fee

        self.grid_orders = {}
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

    def _new_id(self) -> str:
        self._oid += 1
        return f"bt_{self._oid}"

    def order_size(self, base_price: float) -> float:
        size_usdt = (self.capital / 2) / self.levels
        return size_usdt / base_price

    def place_grid(self, price: float, timestamp: int):
        self.grid_orders = {}
        self.grid_base_price = price
        size = self.order_size(price)
        for i in range(1, self.levels + 1):
            buy_id = self._new_id()
            self.grid_orders[buy_id] = {
                "side": "buy",
                "price": price * (1 - i * self.spread),
                "size": size,
                "is_counter": False,
            }
            sell_id = self._new_id()
            self.grid_orders[sell_id] = {
                "side": "sell",
                "price": price * (1 + i * self.spread),
                "size": size,
                "is_counter": False,
            }

    def process_candle(self, candle: list):
        """Simule une bougie: [timestamp, open, high, low, close, volume]."""
        ts, o, h, l, c, v = candle
        if self.stopped:
            return

        # Premier placement
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

            fee = fill_price * size * self.fee
            self.total_fees += fee

            profit = 0.0
            if is_counter:
                profit = fill_price * self.spread * size - fee
                self.total_profit += profit
                self.cycles_completed += 1
            else:
                self.total_profit -= fee

            self.total_trades += 1
            self.fills.append({
                "time": ts, "side": side, "price": fill_price,
                "profit": profit, "is_counter": is_counter,
            })

            # Contre-ordre
            if side == "buy":
                counter_price = fill_price * (1 + self.spread)
                new_id = self._new_id()
                self.grid_orders[new_id] = {
                    "side": "sell", "price": counter_price,
                    "size": size, "is_counter": True,
                }
            else:
                counter_price = fill_price * (1 - self.spread)
                new_id = self._new_id()
                self.grid_orders[new_id] = {
                    "side": "buy", "price": counter_price,
                    "size": size, "is_counter": True,
                }

        # Equity tracking
        equity = self.capital + self.total_profit
        self.equity_curve.append((ts, equity))
        if equity > self.max_equity:
            self.max_equity = equity
        dd = (self.max_equity - equity) / self.max_equity if self.max_equity > 0 else 0
        if dd > self.max_drawdown:
            self.max_drawdown = dd

        # Stop loss
        if self.total_profit < -(self.capital * self.stop_loss_pct):
            self.stopped = True
            return

        # Recentrage
        if self.grid_base_price:
            drift = abs(c - self.grid_base_price) / self.grid_base_price
            if drift > self.range_pct:
                self.place_grid(c, ts)


def display_results(bt: GridBacktester, candles: list, args):
    start_price = candles[0][1]
    end_price = candles[-1][4]
    hold_return = (end_price - start_price) / start_price * 100
    start_date = datetime.fromtimestamp(candles[0][0] / 1000, tz=timezone.utc)
    end_date = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc)

    roi = (bt.total_profit / bt.capital * 100) if bt.capital else 0
    daily_roi = roi / args.days if args.days else 0

    # Config panel
    config = Table(show_header=False, expand=True, padding=(0, 1))
    config.add_column("Param", style="dim")
    config.add_column("Valeur", style="bold")
    config.add_row("Paire", args.symbol)
    config.add_row("Periode", f"{start_date:%Y-%m-%d} -> {end_date:%Y-%m-%d} ({args.days}j)")
    config.add_row("Capital", f"{bt.capital:.2f} USDT")
    config.add_row("Niveaux", f"{bt.levels} x2 = {bt.levels * 2} ordres")
    config.add_row("Spread", f"{bt.spread * 100:.2f}%")
    config.add_row("Frais", f"{bt.fee * 100:.2f}%")
    config.add_row("Bougies", f"{len(candles)} ({args.timeframe})")

    console.print(Panel(config, title="[bold]Configuration[/bold]", border_style="blue"))

    # Results panel
    results = Table(show_header=False, expand=True, padding=(0, 1))
    results.add_column("Metric", style="dim")
    results.add_column("Valeur")

    profit_color = "green" if bt.total_profit >= 0 else "red"
    results.add_row("Profit net", Text(f"{bt.total_profit:+.4f} USDT", style=f"bold {profit_color}"))
    results.add_row("ROI", Text(f"{roi:+.2f}%", style=f"bold {profit_color}"))
    results.add_row("ROI/jour", Text(f"{daily_roi:+.3f}%/j", style=profit_color))
    results.add_row("ROI projete/mois", Text(f"{daily_roi * 30:+.2f}%/mois", style=profit_color))
    results.add_row("", "")
    results.add_row("Trades total", f"{bt.total_trades}")
    results.add_row("Cycles completes", f"{bt.cycles_completed}")
    results.add_row("Frais total", Text(f"-{bt.total_fees:.4f} USDT", style="red"))
    results.add_row("Max drawdown", Text(f"{bt.max_drawdown * 100:.2f}%", style="yellow"))
    if bt.stopped:
        results.add_row("Stop loss", Text("DECLENCHE", style="bold red"))
    results.add_row("", "")
    results.add_row("Buy & Hold", Text(f"{hold_return:+.2f}%", style="cyan"))
    grid_vs_hold = roi - hold_return
    cmp_color = "green" if grid_vs_hold >= 0 else "red"
    results.add_row("Grid vs Hold", Text(f"{grid_vs_hold:+.2f}%", style=f"bold {cmp_color}"))

    console.print(Panel(results, title="[bold]Resultats[/bold]", border_style="green"))

    # Equity sparkline (simple text-based)
    if bt.equity_curve:
        equities = [e[1] for e in bt.equity_curve]
        min_eq = min(equities)
        max_eq = max(equities)
        width = min(80, console.width - 10)
        step = max(1, len(equities) // width)
        sampled = equities[::step][:width]

        bars = "▁▂▃▄▅▆▇█"
        if max_eq > min_eq:
            line = ""
            for val in sampled:
                idx = int((val - min_eq) / (max_eq - min_eq) * (len(bars) - 1))
                color = "green" if val >= bt.capital else "red"
                line += f"[{color}]{bars[idx]}[/{color}]"
        else:
            line = bars[4] * len(sampled)

        equity_text = Text.from_markup(
            f"[dim]{min_eq:.2f}[/] {line} [dim]{max_eq:.2f}[/]"
        )
        console.print(Panel(equity_text, title="[bold]Equity curve[/bold]", border_style="yellow"))

    # Top fills
    profitable_fills = [f for f in bt.fills if f["profit"] > 0]
    if profitable_fills:
        profitable_fills.sort(key=lambda f: f["profit"], reverse=True)
        top = profitable_fills[:5]
        fills_table = Table(show_header=True, header_style="bold", expand=True, padding=(0, 1))
        fills_table.add_column("Date", style="dim")
        fills_table.add_column("Side", justify="center")
        fills_table.add_column("Prix", justify="right")
        fills_table.add_column("Profit", justify="right", style="green")
        for f in top:
            dt = datetime.fromtimestamp(f["time"] / 1000, tz=timezone.utc)
            fills_table.add_row(
                f"{dt:%Y-%m-%d %H:%M}",
                f["side"].upper(),
                f"{f['price']:.2f}",
                f"+{f['profit']:.4f}",
            )
        console.print(Panel(fills_table, title=f"[bold]Top 5 fills[/bold]", border_style="magenta"))


def main():
    args = parse_args()

    console.print(Panel(
        f"[bold cyan]Grid Backtester[/]\n"
        f"[dim]{args.symbol} | {args.days} jours | {args.levels} niveaux | spread {args.spread*100:.2f}%[/]",
        border_style="blue",
    ))

    # Init exchange (public API only, no keys needed for OHLCV)
    try:
        exchange_class = getattr(ccxt, args.exchange)
        exchange = exchange_class({"enableRateLimit": True})
        exchange.load_markets()
    except Exception as e:
        console.print(f"[bold red]Erreur connexion {args.exchange}: {e}[/]")
        sys.exit(1)

    if args.symbol not in exchange.markets:
        console.print(f"[bold red]Paire {args.symbol} non disponible sur {args.exchange}[/]")
        sys.exit(1)

    # Fetch candles
    candles = fetch_candles(exchange, args.symbol, args.timeframe, args.days)
    if len(candles) < 10:
        console.print("[bold red]Pas assez de donnees pour backtester[/]")
        sys.exit(1)

    console.print(f"[green]{len(candles)} bougies chargees[/]\n")

    # Run backtest
    bt = GridBacktester(
        capital=args.capital,
        levels=args.levels,
        spread=args.spread,
        range_pct=args.range_pct,
        stop_loss_pct=args.stop_loss,
        fee=args.fee,
    )

    with Progress(console=console) as progress:
        task = progress.add_task("[cyan]Simulation...", total=len(candles))
        for candle in candles:
            bt.process_candle(candle)
            progress.advance(task)

    console.print()
    display_results(bt, candles, args)


if __name__ == "__main__":
    main()
