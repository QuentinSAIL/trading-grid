#!/usr/bin/env python3
"""
Grid Bot — Parameter Sweep
Teste des centaines de configs et trouve la meilleure.

Usage:
    python sweep.py 200         # sweep sur 200 jours
    python sweep.py 1000        # sweep sur 1000 jours
"""

import json
import sys
import time
import itertools
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import ccxt
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import (Progress, BarColumn, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn,
                           MofNCompleteColumn, SpinnerColumn)

from backtest import GridBacktester, fetch_candles

TZ = ZoneInfo("Europe/Paris")
console = Console(force_terminal=True, width=250)


# --- Parametres a tester ---

# Mini-sweep H: Validation - saturation RSI/weight + test 200j
# Best G: spread=0.007, levels=3, weight=-0.8, RSI=3.0, EMA=off
# stale=72/0.0008, inv=0.20, btc=0.25
PARAM_GRID = {
    "spread":        [0.006, 0.007, 0.008],
    "levels":        [3],
    "weight_factor": [-0.9, -0.8, -0.7, -0.5],
    "range_pct":     [0.04, 0.05],
    "rsi_period":    [14],
    "rsi_strength":  [2.5, 3.0, 4.0, 5.0],
    "ema_fast":      [12],
    "ema_slow":      [26],
    "ema_strength":  [0],
    "bb_period":     [20],
    "bb_mult":       [2.0],
    "bb_spread":     [True],
    "stale_hours":   [72],
    "decay_per_hour":[0.0005, 0.0008, 0.001],
    "trend_spread_mult": [0],
    "dd_threshold":  [1.0],
    "dd_factor":     [0.5],
    "max_inv_ratio": [0.15, 0.20],
    "initial_btc_pct": [0.20, 0.25],
    "stop_loss":     [0.25],
    "rebalance_every": [0],
}
# 3*1*4*2*4*3*3*2*2 = 3456 configs
GRID_TYPE = "geometric"

CAPITAL = 100.0
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
        grid_type=GRID_TYPE,
        weight_factor=kwargs["weight_factor"],
        rsi_period=kwargs["rsi_period"],
        rsi_strength=kwargs["rsi_strength"],
        ema_fast=kwargs["ema_fast"],
        ema_slow=kwargs["ema_slow"],
        ema_strength=kwargs["ema_strength"],
        bb_period=kwargs["bb_period"],
        bb_mult=kwargs["bb_mult"],
        bb_spread_adapt=kwargs["bb_spread"],
        stale_hours=kwargs.get("stale_hours", 0),
        decay_per_hour=kwargs.get("decay_per_hour", 0.0002),
        trend_spread_mult=kwargs.get("trend_spread_mult", 0.0),
        dd_threshold=kwargs.get("dd_threshold", 1.0),
        dd_factor=kwargs.get("dd_factor", 0.5),
        max_inv_ratio=kwargs.get("max_inv_ratio", 1.0),
        initial_btc_pct=kwargs.get("initial_btc_pct", 0.5),
        trend_liquidation=kwargs.get("trend_liquidation", 0.0),
        rebalance_every=kwargs.get("rebalance_every", 0),
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
        "stop_loss": kwargs["stop_loss"],
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

    # Generer toutes les combinaisons (filtrer ema_fast >= ema_slow)
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    all_combos = list(itertools.product(*values))
    ema_fast_idx = keys.index("ema_fast")
    ema_slow_idx = keys.index("ema_slow")
    combos = [c for c in all_combos
              if c[ema_fast_idx] < c[ema_slow_idx]  # fast must be < slow
              or c[keys.index("ema_strength")] == 0]  # unless EMA is off
    console.print(f"[yellow]{len(combos)} configurations a tester[/] "
                  f"[dim](filtre {len(all_combos) - len(combos)} combos EMA invalides)[/]\n")

    results = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("[bold]{task.percentage:>5.1f}%"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Sweep", total=len(combos))
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
    table.add_column("Sprd", justify="right")
    table.add_column("Lvl", justify="right")
    table.add_column("Wgt", justify="right")
    table.add_column("Rng", justify="right")
    table.add_column("RSI", justify="center")
    table.add_column("EMA", justify="center")
    table.add_column("Stale", justify="center")
    table.add_column("TrSp", justify="center")
    table.add_column("DD/Inv", justify="center")
    table.add_column("Stop", justify="center")
    table.add_column("Reb", justify="center")

    for i, r in enumerate(results[:20]):
        roi_style = "green" if r["roi"] >= 0 else "red"
        vs_style = "green" if r["vs_hold"] >= 0 else "red"
        rsi_str = "-" if r["rsi_strength"] == 0 else f"{r['rsi_period']}/{r['rsi_strength']}"
        ema_str = "-" if r["ema_strength"] == 0 else f"{r['ema_fast']}/{r['ema_slow']}x{r['ema_strength']}"
        stale_str = "-" if r.get("stale_hours", 0) == 0 else f"{r['stale_hours']}h"
        trsp_str = "-" if r.get("trend_spread_mult", 0) == 0 else f"{r['trend_spread_mult']:.0f}x"
        dd_t = r.get("dd_threshold", 1.0)
        inv_r = r.get("max_inv_ratio", 1.0)
        dd_inv = ""
        if dd_t < 1.0:
            dd_inv += f"D{dd_t*100:.0f}"
        if inv_r < 1.0:
            dd_inv += f"I{inv_r*100:.0f}"
        if not dd_inv:
            dd_inv = "-"
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
            f"{r['weight_factor']:.0f}",
            f"{r['range_pct']*100:.0f}%",
            rsi_str,
            ema_str,
            stale_str,
            trsp_str,
            dd_inv,
            f"[red]{r['stop_loss']*100:.0f}%X[/]" if r["stopped"] else f"[green]{r['stop_loss']*100:.0f}%[/]",
            f"{r.get('rebalance_every', 0)}h" if r.get('rebalance_every', 0) > 0 else "-",
        )

    console.print(table)

    # Worst 5 pour comparaison
    console.print()
    worst = Table(title=f"Worst 5 configs ({days}j)", show_lines=True)
    worst.add_column("#", style="dim", width=3)
    worst.add_column("ROI", justify="right", style="bold red")
    worst.add_column("Spread", justify="right")
    worst.add_column("Lvl", justify="right")
    worst.add_column("Wgt", justify="right")
    worst.add_column("DD", justify="right")
    worst.add_column("RSI")
    worst.add_column("EMA")

    for i, r in enumerate(results[-5:]):
        rsi_str = "-" if r["rsi_strength"] == 0 else f"{r['rsi_period']}/{r['rsi_strength']}"
        ema_str = "-" if r["ema_strength"] == 0 else f"{r['ema_fast']}/{r['ema_slow']}x{r['ema_strength']}"
        worst.add_row(
            str(len(results) - 4 + i),
            f"{r['roi']:+.1f}%",
            f"{r['spread']*100:.1f}%",
            str(r["levels"]),
            f"{r['weight_factor']:.1f}",
            f"{r['max_dd']:.1f}%",
            rsi_str,
            ema_str,
        )
    console.print(worst)

    # Meilleure config
    def _fmt_config(r):
        rsi_str = "off" if r["rsi_strength"] == 0 else f'{r["rsi_period"]}p / {r["rsi_strength"]}x'
        ema_str = "off" if r["ema_strength"] == 0 else f'{r["ema_fast"]}/{r["ema_slow"]} / {r["ema_strength"]}x'
        bb_str = "off" if not r["bb_spread"] else f'{r["bb_period"]}p / {r["bb_mult"]}x'
        stale_str = "off" if r.get("stale_hours", 0) == 0 else f'{r["stale_hours"]}h / {r["decay_per_hour"]*100:.02f}%/h'
        return (
            f"GRID_SPREAD={r['spread']}\n"
            f"GRID_LEVELS={r['levels']}\n"
            f"GRID_TYPE={GRID_TYPE}\n"
            f"WEIGHT_FACTOR={r['weight_factor']}\n"
            f"PRICE_RANGE_PCT={r['range_pct']}\n"
            f"STOP_LOSS_PCT={r['stop_loss']}\n"
            f"RSI_PERIOD={r['rsi_period']}\n"
            f"RSI_STRENGTH={r['rsi_strength']}\n"
            f"EMA_FAST={r['ema_fast']}\n"
            f"EMA_SLOW={r['ema_slow']}\n"
            f"EMA_STRENGTH={r['ema_strength']}\n"
            f"BB_PERIOD={r['bb_period']}\n"
            f"BB_MULT={r['bb_mult']}\n"
            f"BB_SPREAD_ADAPT={'true' if r['bb_spread'] else 'false'}\n"
            f"STALE_HOURS={r.get('stale_hours', 0)}\n"
            f"DECAY_PER_HOUR={r.get('decay_per_hour', 0.0002)}\n"
            f"TREND_SPREAD_MULT={r.get('trend_spread_mult', 0.0)}\n"
            f"DD_THRESHOLD={r.get('dd_threshold', 1.0)}\n"
            f"DD_FACTOR={r.get('dd_factor', 0.5)}\n"
            f"MAX_INV_RATIO={r.get('max_inv_ratio', 1.0)}\n"
            f"INITIAL_BTC_PCT={r.get('initial_btc_pct', 0.5)}\n"
            f"TREND_LIQUIDATION={r.get('trend_liquidation', 0.0)}\n"
            f"REBALANCE_EVERY={r.get('rebalance_every', 0)}\n"
            f"\nRSI: {rsi_str} | EMA: {ema_str} | BB: {bb_str} | Stale: {stale_str}"
        )

    best = results[0]
    console.print(Panel(
        f"[bold green]MEILLEURE CONFIG[/]\n\n"
        f"{_fmt_config(best)}\n\n"
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
            f"{_fmt_config(rel)}\n\n"
            f"ROI: {rel['roi']:+.2f}% | "
            f"PnL: {rel['pnl']:+.2f} USDT | "
            f"Max DD: {rel['max_dd']:.1f}% | "
            f"Sharpe~: {rel['sharpe_approx']:.2f}",
            border_style="cyan",
        ))

    # Export JSON des top 20 pour analyse facile
    top20 = []
    for r in results[:20]:
        top20.append({k: v for k, v in r.items()
                      if isinstance(v, (int, float, bool, str))})
    with open("/tmp/sweep_results.json", "w") as f:
        json.dump(top20, f, indent=2)
    console.print("[dim]Resultats exportes dans /tmp/sweep_results.json[/]")


if __name__ == "__main__":
    main()
