#!/usr/bin/env python3
"""
Grid Bot — Dashboard CLI live
Affiche l'état du bot en temps réel depuis le fichier state JSON.
Auto-refresh toutes les 2 secondes.

Usage:
    python dashboard.py                     # state par défaut
    python dashboard.py /path/to/state.json # state custom
"""

import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

from dotenv import load_dotenv
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

load_dotenv()

STATE_FILE = sys.argv[1] if len(sys.argv) > 1 else os.getenv("STATE_FILE", "data/bot_state.json")
SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
GRID_SPREAD = float(os.getenv("GRID_SPREAD", 0.005))
REFRESH_INTERVAL = 2


def load_state() -> dict | None:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def format_elapsed(start_time_str: str) -> str:
    try:
        start = datetime.fromisoformat(start_time_str)
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        delta = datetime.now(TZ) - start
        total_s = int(delta.total_seconds())
        d = total_s // 86400
        h = (total_s % 86400) // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        if d > 0:
            return f"{d}j {h}h{m:02d}m"
        return f"{h}h{m:02d}m{s:02d}s"
    except Exception:
        return "N/A"


def build_header(state: dict) -> Panel:
    price = state.get("current_price") or state.get("grid_base_price") or 0
    base_price = state.get("grid_base_price") or 0
    drift = abs(price - base_price) / base_price * 100 if base_price else 0
    elapsed = format_elapsed(state.get("start_time", ""))
    profit = state.get("total_profit", 0)
    trades = state.get("total_trades", 0)
    start_val = state.get("start_portfolio_value") or 1
    roi = profit / start_val * 100
    active = len(state.get("grid_orders", {}))
    portfolio = state.get("portfolio_value", 0)
    capital = state.get("effective_capital", 0)
    alloc = state.get("capital_allocation", 0)

    profit_color = "green" if profit >= 0 else "red"
    roi_color = "green" if roi >= 0 else "red"

    header = Text()
    header.append(f"  {SYMBOL}", style="bold cyan")
    header.append(f"  |  Prix: ", style="dim")
    header.append(f"{price:.2f}", style="bold white")
    header.append(f"  |  Base: ", style="dim")
    header.append(f"{base_price:.2f}", style="white")
    header.append(f"  |  Drift: ", style="dim")
    drift_style = "yellow" if drift > 3 else "green"
    header.append(f"{drift:.2f}%", style=drift_style)
    header.append(f"\n  Profit: ", style="dim")
    header.append(f"{profit:+.4f} USDT", style=f"bold {profit_color}")
    header.append(f"  |  ROI: ", style="dim")
    header.append(f"{roi:+.2f}%", style=f"bold {roi_color}")
    header.append(f"  |  Trades: ", style="dim")
    header.append(f"{trades}", style="bold white")
    header.append(f"  |  Ordres: ", style="dim")
    header.append(f"{active}", style="bold white")
    header.append(f"  |  Uptime: ", style="dim")
    header.append(f"{elapsed}", style="white")
    header.append(f"\n  Portefeuille: ", style="dim")
    header.append(f"{portfolio:.2f} USDT", style="bold white")
    header.append(f"  |  Capital alloue: ", style="dim")
    header.append(f"{capital:.2f} USDT", style="bold cyan")
    header.append(f" ({alloc:.0f}%)", style="dim")

    return Panel(header, title="[bold]Grid Trading Bot[/bold]", border_style="blue")


def build_grid_table(state: dict) -> Panel:
    grid_orders = state.get("grid_orders", {})
    price = state.get("current_price") or state.get("grid_base_price") or 0

    buys = []
    sells = []
    for order in grid_orders.values():
        if order["side"] == "buy":
            buys.append(order)
        else:
            sells.append(order)

    buys.sort(key=lambda o: o["price"], reverse=True)
    sells.sort(key=lambda o: o["price"])

    table = Table(show_header=True, header_style="bold", expand=True, padding=(0, 1))
    table.add_column("SELL", style="red", justify="right", ratio=1)
    table.add_column("Prix", style="red dim", justify="right", ratio=1)
    table.add_column("", justify="center", width=5)
    table.add_column("Prix", style="green dim", justify="left", ratio=1)
    table.add_column("BUY", style="green", justify="left", ratio=1)

    max_rows = max(len(buys), len(sells))
    for i in range(max_rows):
        sell_size = ""
        sell_price = ""
        buy_size = ""
        buy_price = ""
        sell_style = ""
        buy_style = ""

        if i < len(sells):
            s = sells[i]
            sell_price = f"{s['price']:.2f}"
            sell_size = f"{s['size']:.6f}"
            sell_style = "bold red" if s.get("is_counter") else "red"

        if i < len(buys):
            b = buys[i]
            buy_price = f"{b['price']:.2f}"
            buy_size = f"{b['size']:.6f}"
            buy_style = "bold green" if b.get("is_counter") else "green"

        sep = ""
        if i == 0:
            sep = f"[bold yellow]{'>' * 3}[/]"

        table.add_row(
            Text(sell_size, style=sell_style) if sell_size else "",
            Text(sell_price, style=sell_style) if sell_price else "",
            sep,
            Text(buy_price, style=buy_style) if buy_price else "",
            Text(buy_size, style=buy_style) if buy_size else "",
        )

    subtitle = f"[dim]Spread: {GRID_SPREAD*100:.2f}%  |  Prix actuel: [bold yellow]{price:.2f}[/][/dim]"
    return Panel(table, title="[bold]Grille[/bold]", subtitle=subtitle, border_style="yellow")


def build_balance_panel(state: dict) -> Panel:
    balance = state.get("balance", {})
    price = state.get("current_price") or state.get("grid_base_price") or 0
    base_asset = SYMBOL.split("/")[0]
    quote_asset = SYMBOL.split("/")[1]

    table = Table(show_header=True, header_style="bold", expand=True, padding=(0, 1))
    table.add_column("Asset", style="cyan", justify="left")
    table.add_column("Libre", justify="right")
    table.add_column("En ordres", justify="right")
    table.add_column("Total", style="bold", justify="right")
    table.add_column("Valeur USDT", style="bold yellow", justify="right")

    quote = balance.get(quote_asset, {})
    q_free = quote.get("free", 0)
    q_used = quote.get("used", 0)
    q_total = quote.get("total", 0)
    table.add_row(
        quote_asset,
        f"{q_free:.2f}",
        f"{q_used:.2f}",
        f"{q_total:.2f}",
        f"{q_total:.2f}",
    )

    base = balance.get(base_asset, {})
    b_free = base.get("free", 0)
    b_used = base.get("used", 0)
    b_total = base.get("total", 0)
    b_value = b_total * price if price else 0
    table.add_row(
        base_asset,
        f"{b_free:.6f}",
        f"{b_used:.6f}",
        f"{b_total:.6f}",
        f"{b_value:.2f}",
    )

    total_value = q_total + b_value
    table.add_row("", "", "", "[bold]Total[/bold]", f"[bold]{total_value:.2f}[/bold]")

    start_val = state.get("start_portfolio_value") or total_value
    pnl = total_value - start_val if total_value > 0 else 0
    pnl_color = "green" if pnl >= 0 else "red"
    subtitle = f"[dim]Depart: {start_val:.2f} USDT  |  PnL: [{pnl_color}]{pnl:+.2f} USDT[/{pnl_color}][/dim]"
    return Panel(table, title=f"[bold]Solde {SYMBOL.split('/')[1]}[/bold]", subtitle=subtitle, border_style="green")


def build_fills_table(state: dict) -> Panel:
    fills = state.get("filled_orders", [])
    recent = fills[-15:] if fills else []
    recent.reverse()

    table = Table(show_header=True, header_style="bold", expand=True, padding=(0, 1))
    table.add_column("Heure", style="dim", justify="left")
    table.add_column("Side", justify="center")
    table.add_column("Prix", justify="right")
    table.add_column("Taille", justify="right")
    table.add_column("Profit", justify="right")
    table.add_column("Type", justify="center")

    for fill in recent:
        side = fill.get("side", "?")
        side_style = "green" if side == "buy" else "red"
        profit = fill.get("profit", 0)
        profit_str = f"+{profit:.4f}" if profit > 0 else "-"
        profit_style = "bold green" if profit > 0 else "dim"
        is_counter = fill.get("is_counter", False)
        type_str = "cycle" if is_counter else "init"
        type_style = "bold cyan" if is_counter else "dim"

        fill_time = fill.get("fill_time", "")
        try:
            t = datetime.fromisoformat(fill_time)
            time_str = t.strftime("%H:%M:%S")
        except Exception:
            time_str = "?"

        table.add_row(
            time_str,
            Text(side.upper(), style=f"bold {side_style}"),
            f"{fill.get('price', 0):.2f}",
            f"{fill.get('size', 0):.6f}",
            Text(profit_str, style=profit_style),
            Text(type_str, style=type_style),
        )

    if not recent:
        table.add_row("", "", "[dim]Aucun fill[/dim]", "", "", "")

    return Panel(table, title=f"[bold]Derniers fills ({len(fills)} total)[/bold]", border_style="magenta")


def build_dashboard(state: dict) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=7),
        Layout(name="body"),
        Layout(name="footer", size=5),
    )
    layout["body"].split_row(
        Layout(name="grid", ratio=3),
        Layout(name="fills", ratio=2),
    )

    layout["header"].update(build_header(state))
    layout["grid"].update(build_grid_table(state))
    layout["fills"].update(build_fills_table(state))
    layout["footer"].update(build_balance_panel(state))

    return layout


def main():
    console = Console()

    if not os.path.exists(STATE_FILE):
        console.print(f"[bold red]Fichier state introuvable: {STATE_FILE}[/]")
        console.print("[dim]Le bot doit tourner pour generer le fichier state.[/]")
        console.print(f"[dim]Usage: python dashboard.py [path/to/bot_state.json][/]")
        sys.exit(1)

    console.print(f"[bold blue]Dashboard Grid Bot[/] — [dim]{STATE_FILE}[/]")
    console.print(f"[dim]Refresh: {REFRESH_INTERVAL}s | Ctrl+C pour quitter[/]\n")

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            state = load_state()
            if state is None:
                live.update(Panel("[bold red]Impossible de lire le state[/]", title="Erreur"))
            else:
                live.update(build_dashboard(state))
            time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
