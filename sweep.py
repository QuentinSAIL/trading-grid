#!/usr/bin/env python3
"""
Grid Bot — Parameter Sweep
Teste des centaines de configs et trouve la meilleure.

Usage:
    python sweep.py 200         # sweep sur 200 jours
    python sweep.py 1000        # sweep sur 1000 jours
"""

import sys
import time
import itertools
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import ccxt
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress

from backtest import GridBacktester, fetch_candles

TZ = ZoneInfo("Europe/Paris")
console = Console()


# --- Parametres a tester ---

PARAM_GRID = {
    "spread":        [0.003, 0.005, 0.008, 0.01, 0.015, 0.02],
    "levels":        [5, 8, 10, 15, 20],
    "grid_type":     ["linear", "geometric"],
    "weight_factor": [0, 0.5, 1.0, 1.5, 2.0, 3.0],
    "range_pct":     [0.02, 0.03, 0.05, 0.08],
    "stop_loss":     [0.50, 1.0],
}

CAPITAL = 80.0
MAKER_FEE = 0.0
TAKER_FEE = 0.001


def run_single(candles, **kwargs):
    """Run un backtest et retourne les metriques cles."""
    bt = GridBacktester(
        capital=CAPITAL,
        levels=kwargs["levels"],
        spread=kwargs["spread"],
        range_pct=kwargs["range_pct"],
        stop_loss_pct=kwargs["stop_loss"],
        maker_fee=MAKER_FEE,
        taker_fee=TAKER_FEE,
        grid_type=kwargs["grid_type"],
        weight_factor=kwargs["weight_factor"],
    )

    for candle in candles:
        bt.process_candle(candle)

    end_price = candles[-1][4]
    start_price = candles[0][1]
    final_value = bt.capital + bt.btc_held * end_price
    pnl = final_value - bt.initial_capital
    roi = (pnl / bt.initial_capital * 100) if bt.initial_capital else 0
    hold_return = (end_price - start_price) / start_price * 100
    days = len(candles) / 24

    return {
        "roi": roi,
        "pnl": pnl,
        "grid_profit": bt.total_profit,
        "final_value": final_value,
        "trades": bt.total_trades,
        "cycles": bt.cycles_completed,
        "max_dd": bt.max_drawdown * 100,
        "stopped": bt.stopped,
        "hold": hold_return,
        "vs_hold": roi - hold_return,
        "daily_roi": roi / days if days > 0 else 0,
        "sharpe_approx": (roi / (bt.max_drawdown * 100))
                         if bt.max_drawdown > 0 else 0,
        **kwargs,
    }


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    symbol = "BTC/USDT"

    console.print(Panel(
        f"[bold cyan]Grid Parameter Sweep[/]\n"
        f"[dim]{symbol} | {days} jours | {CAPITAL} USDT[/]",
        border_style="blue",
    ))

    # Telecharger les bougies une seule fois
    exchange = ccxt.mexc({"enableRateLimit": True})
    exchange.load_markets()
    candles = fetch_candles(exchange, symbol, "1h", days)
    console.print(f"[green]{len(candles)} bougies chargees[/]\n")

    # Generer toutes les combinaisons
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))
    console.print(f"[yellow]{len(combos)} configurations a tester[/]\n")

    results = []
    with Progress(console=console) as progress:
        task = progress.add_task("[cyan]Sweep...", total=len(combos))
        for combo in combos:
            params = dict(zip(keys, combo))
            try:
                result = run_single(candles, **params)
                results.append(result)
            except Exception:
                pass
            progress.advance(task)

    # Trier par ROI
    results.sort(key=lambda r: r["roi"], reverse=True)

    # Top 20
    table = Table(title=f"Top 20 configs ({days}j)", show_lines=True,
                  expand=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("ROI", justify="right", style="bold")
    table.add_column("PnL", justify="right")
    table.add_column("Grid$", justify="right")
    table.add_column("DD", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("vs Hold", justify="right")
    table.add_column("ROI/j", justify="right")
    table.add_column("Spread", justify="right")
    table.add_column("Lvl", justify="right")
    table.add_column("Type", justify="center")
    table.add_column("Wgt", justify="right")
    table.add_column("Range", justify="right")
    table.add_column("SL", justify="right")
    table.add_column("Stop", justify="center")

    for i, r in enumerate(results[:20]):
        roi_style = "green" if r["roi"] >= 0 else "red"
        vs_style = "green" if r["vs_hold"] >= 0 else "red"
        table.add_row(
            str(i + 1),
            f"[{roi_style}]{r['roi']:+.1f}%[/]",
            f"{r['pnl']:+.1f}",
            f"{r['grid_profit']:+.1f}",
            f"{r['max_dd']:.1f}%",
            str(r["trades"]),
            f"[{vs_style}]{r['vs_hold']:+.1f}%[/]",
            f"{r['daily_roi']:+.3f}%",
            f"{r['spread']*100:.1f}%",
            str(r["levels"]),
            r["grid_type"][:3],
            f"{r['weight_factor']:.1f}",
            f"{r['range_pct']*100:.0f}%",
            f"{r['stop_loss']*100:.0f}%",
            "[red]X[/]" if r["stopped"] else "[green]OK[/]",
        )

    console.print(table)

    # Worst 5 pour comparaison
    console.print()
    worst = Table(title=f"Worst 5 configs ({days}j)", show_lines=True)
    worst.add_column("#", style="dim", width=3)
    worst.add_column("ROI", justify="right", style="bold red")
    worst.add_column("Spread", justify="right")
    worst.add_column("Lvl", justify="right")
    worst.add_column("Type")
    worst.add_column("Wgt", justify="right")
    worst.add_column("DD", justify="right")

    for i, r in enumerate(results[-5:]):
        worst.add_row(
            str(len(results) - 4 + i),
            f"{r['roi']:+.1f}%",
            f"{r['spread']*100:.1f}%",
            str(r["levels"]),
            r["grid_type"][:3],
            f"{r['weight_factor']:.1f}",
            f"{r['max_dd']:.1f}%",
        )
    console.print(worst)

    # Meilleure config
    best = results[0]
    console.print(Panel(
        f"[bold green]MEILLEURE CONFIG[/]\n\n"
        f"GRID_SPREAD={best['spread']}\n"
        f"GRID_LEVELS={best['levels']}\n"
        f"GRID_TYPE={best['grid_type']}\n"
        f"WEIGHT_FACTOR={best['weight_factor']}\n"
        f"PRICE_RANGE_PCT={best['range_pct']}\n"
        f"STOP_LOSS_PCT={best['stop_loss']}\n\n"
        f"ROI: {best['roi']:+.2f}% | "
        f"PnL: {best['pnl']:+.2f} USDT | "
        f"Max DD: {best['max_dd']:.1f}% | "
        f"Trades: {best['trades']}",
        border_style="green",
    ))

    # Config la plus FIABLE: meilleur ratio ROI/drawdown sans stop loss
    reliable = [r for r in results
                if not r["stopped"] and r["max_dd"] < 40]
    if reliable:
        reliable.sort(key=lambda r: r["sharpe_approx"], reverse=True)
        rel = reliable[0]
        console.print(Panel(
            f"[bold cyan]CONFIG LA PLUS FIABLE[/] "
            f"(meilleur ROI/DD, pas de stop)\n\n"
            f"GRID_SPREAD={rel['spread']}\n"
            f"GRID_LEVELS={rel['levels']}\n"
            f"GRID_TYPE={rel['grid_type']}\n"
            f"WEIGHT_FACTOR={rel['weight_factor']}\n"
            f"PRICE_RANGE_PCT={rel['range_pct']}\n"
            f"STOP_LOSS_PCT={rel['stop_loss']}\n\n"
            f"ROI: {rel['roi']:+.2f}% | "
            f"PnL: {rel['pnl']:+.2f} USDT | "
            f"Max DD: {rel['max_dd']:.1f}% | "
            f"Sharpe~: {rel['sharpe_approx']:.2f}",
            border_style="cyan",
        ))


if __name__ == "__main__":
    main()
