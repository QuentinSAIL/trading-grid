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
from rich.columns import Columns
from rich.console import Console, Group
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

# ── Braille chart characters ─────────────────────────────────────────
# Each braille char encodes a 2x4 dot grid. We use columns of 4 rows.
BRAILLE_BASE = 0x2800
BRAILLE_DOTS = [
    [0x01, 0x02, 0x04, 0x40],  # left column  (dots 1,2,3,7)
    [0x08, 0x10, 0x20, 0x80],  # right column  (dots 4,5,6,8)
]


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


def make_bar(value: float, max_val: float, width: int = 20, color: str = "cyan") -> Text:
    """Create a horizontal bar using block characters."""
    if max_val <= 0:
        filled = 0
    else:
        ratio = min(value / max_val, 1.0)
        filled = int(ratio * width)
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="bright_black")
    return bar


def build_price_chart(state: dict, width: int = 80, height: int = 18) -> Panel:
    """Build an ASCII candlestick chart with order markers and grid levels."""
    candles = state.get("_candles", [])
    grid_orders = state.get("grid_orders", {})
    fills = state.get("filled_orders", [])
    price = state.get("current_price") or state.get("grid_base_price") or 0

    if not candles or len(candles) < 2:
        lines = []
        lines.append("")
        lines.append("       ╭─────────────────────────────────────────────╮")
        lines.append("       │                                             │")
        lines.append("       │       En attente de donnees OHLCV...        │")
        lines.append("       │       Les bougies apparaitront bientot      │")
        lines.append("       │                                             │")
        lines.append("       ╰─────────────────────────────────────────────╯")
        txt = Text("\n".join(lines), style="dim italic bright_blue")
        return Panel(txt, title="[bold bright_cyan]  Chandelier  [/bold bright_cyan]",
                     border_style="bright_blue", height=height + 4)

    label_w = 12  # width for price labels on the left
    marker_w = 3  # width for order markers on the right
    chart_w = width - label_w - marker_w - 2  # usable chart columns

    # Each candle takes 2 columns (1 char + 1 space), decide how many to show
    max_candles = chart_w // 2
    display_candles = candles[-max_candles:]

    # Collect all prices for range calculation
    all_highs = [c["h"] for c in display_candles]
    all_lows = [c["l"] for c in display_candles]
    grid_prices = [o["price"] for o in grid_orders.values()]

    p_min = min(all_lows + (grid_prices if grid_prices else all_lows))
    p_max = max(all_highs + (grid_prices if grid_prices else all_highs))
    if price > 0:
        p_min = min(p_min, price)
        p_max = max(p_max, price)

    p_range = p_max - p_min if p_max > p_min else p_max * 0.01 or 1
    # Add 5% padding
    p_min -= p_range * 0.05
    p_max += p_range * 0.05
    p_range = p_max - p_min

    def price_to_row(p):
        row = height - 1 - int((p - p_min) / p_range * (height - 1))
        return max(0, min(height - 1, row))

    # Build character + style grids
    n_cols = len(display_candles) * 2
    chart = [[" " for _ in range(n_cols)] for _ in range(height)]
    styles = [["" for _ in range(n_cols)] for _ in range(height)]

    # Map candle timestamps for fill matching
    candle_times = {}
    for idx, c in enumerate(display_candles):
        candle_times[idx] = (c["t"], c["t"] + 5 * 60 * 1000)  # 5min candles

    # Draw grid levels as dotted horizontal lines
    for gp in grid_prices:
        row = price_to_row(gp)
        side = "sell"
        for o in grid_orders.values():
            if o["price"] == gp:
                side = o["side"]
                break
        line_style = "red dim" if side == "sell" else "green dim"
        for c in range(0, n_cols, 3):
            if chart[row][c] == " ":
                chart[row][c] = "┄"
                styles[row][c] = line_style

    # Draw current price line
    if price > 0:
        row = price_to_row(price)
        for c in range(0, n_cols, 2):
            if chart[row][c] == " ":
                chart[row][c] = "─"
                styles[row][c] = "yellow dim"

    # Build fill lookup: map candle index to list of fills
    fill_at_candle = {}
    for f in fills:
        ft = f.get("fill_time", "")
        try:
            fill_ts = int(datetime.fromisoformat(ft).timestamp() * 1000)
            for ci, (t_start, t_end) in candle_times.items():
                if t_start <= fill_ts < t_end:
                    fill_at_candle.setdefault(ci, []).append(f)
                    break
        except Exception:
            pass

    # Draw candlesticks
    for i, candle in enumerate(display_candles):
        col = i * 2  # each candle at column i*2
        o_price, h_price, l_price, c_price = candle["o"], candle["h"], candle["l"], candle["c"]

        bullish = c_price >= o_price
        color = "bright_green" if bullish else "bright_red"
        body_char = "┃" if bullish else "┃"
        wick_char = "│"

        body_top = price_to_row(max(o_price, c_price))
        body_bot = price_to_row(min(o_price, c_price))
        wick_top = price_to_row(h_price)
        wick_bot = price_to_row(l_price)

        # Draw wick (high to low)
        for r in range(wick_top, wick_bot + 1):
            if chart[r][col] == " " or chart[r][col] in ("┄", "─"):
                chart[r][col] = wick_char
                styles[r][col] = f"{color} dim"

        # Draw body (overwrite wick)
        for r in range(body_top, body_bot + 1):
            chart[r][col] = body_char
            styles[r][col] = f"bold {color}"

        # If body is a single row (doji), use special char
        if body_top == body_bot:
            chart[body_top][col] = "━"
            styles[body_top][col] = f"bold {color}"

        # Draw fill markers on candles
        if i in fill_at_candle:
            for f in fill_at_candle[i]:
                side = f.get("side", "?")
                fp = f.get("price", 0)
                if fp > 0:
                    fr = price_to_row(fp)
                    if col + 1 < n_cols:
                        if side == "buy":
                            chart[fr][col + 1] = "◀"
                            styles[fr][col + 1] = "bold bright_green"
                        else:
                            chart[fr][col + 1] = "◀"
                            styles[fr][col + 1] = "bold bright_red"

    # Build right-side markers for grid orders
    right_markers = {}  # row -> (char, style)
    for o in grid_orders.values():
        row = price_to_row(o["price"])
        if o["side"] == "buy":
            right_markers[row] = ("◁", "green")
        else:
            right_markers[row] = ("▷", "red")
    if price > 0:
        right_markers[price_to_row(price)] = ("◆", "bold yellow")

    # Render to Rich Text
    result = Text()
    n_labels = 5  # number of price labels on the axis
    label_rows = set()
    for li in range(n_labels):
        label_rows.add(int(li * (height - 1) / (n_labels - 1)))

    for r in range(height):
        # Price label
        if r in label_rows:
            p_at_row = p_max - (r / (height - 1)) * p_range
            # Smart formatting based on price magnitude
            if p_at_row >= 1000:
                label = f" {p_at_row:>{label_w - 2},.2f} "
            elif p_at_row >= 1:
                label = f" {p_at_row:>{label_w - 2}.4f} "
            else:
                label = f" {p_at_row:>{label_w - 2}.6f} "
            result.append(label, style="dim cyan")
        else:
            result.append(" " * label_w, style="")

        # Axis line
        result.append("│", style="bright_black")

        # Chart content
        for c in range(min(n_cols, chart_w)):
            ch = chart[r][c]
            st = styles[r][c]
            result.append(ch, style=st if st else "")

        # Pad remaining chart width
        remaining = chart_w - min(n_cols, chart_w)
        if remaining > 0:
            result.append(" " * remaining)

        # Right axis + markers
        result.append("│", style="bright_black")
        if r in right_markers:
            mk_ch, mk_st = right_markers[r]
            result.append(mk_ch, style=mk_st)
        else:
            result.append(" ")

        result.append("\n")

    # Bottom axis
    result.append(" " * label_w, style="")
    result.append("└", style="bright_black")
    result.append("─" * chart_w, style="bright_black")
    result.append("┘", style="bright_black")

    # Time labels on bottom
    result.append("\n" + " " * (label_w + 1), style="")
    if display_candles:
        first_t = display_candles[0].get("t", 0)
        mid_idx = len(display_candles) // 2
        mid_t = display_candles[mid_idx].get("t", 0)
        last_t = display_candles[-1].get("t", 0)
        try:
            t1 = datetime.fromtimestamp(first_t / 1000, tz=TZ).strftime("%H:%M")
            t2 = datetime.fromtimestamp(mid_t / 1000, tz=TZ).strftime("%H:%M")
            t3 = datetime.fromtimestamp(last_t / 1000, tz=TZ).strftime("%H:%M")
            spacing = chart_w // 2
            result.append(f"{t1}", style="dim")
            result.append(" " * max(1, spacing - 5 - len(t1)), style="")
            result.append(f"{t2}", style="dim")
            result.append(" " * max(1, spacing - 5 - len(t2)), style="")
            result.append(f"{t3}", style="dim")
        except Exception:
            pass

    # Legend
    result.append("\n" + " " * (label_w + 1), style="")
    result.append("┃", style="bold bright_green")
    result.append(" Haussier  ", style="dim")
    result.append("┃", style="bold bright_red")
    result.append(" Baissier  ", style="dim")
    result.append("─", style="yellow dim")
    result.append(" Prix actuel  ", style="dim")
    result.append("┄", style="green dim")
    result.append(" Grille buy  ", style="dim")
    result.append("┄", style="red dim")
    result.append(" Grille sell  ", style="dim")
    result.append("◀", style="bold bright_green")
    result.append(" Fill", style="dim")

    # Candle info
    if display_candles:
        last = display_candles[-1]
        chg = ((last["c"] - last["o"]) / last["o"] * 100) if last["o"] else 0
        chg_style = "bright_green" if chg >= 0 else "bright_red"
        chg_icon = "▲" if chg >= 0 else "▼"
        vol_str = f"{last['v']:,.0f}" if last.get("v") else "N/A"
        subtitle = (f"[dim]5min  |  {len(display_candles)} bougies  |  "
                    f"Derniere: [{chg_style}]{chg_icon} {chg:+.2f}%[/{chg_style}]  |  Vol: {vol_str}[/dim]")
    else:
        subtitle = ""

    return Panel(result,
                 title="[bold bright_cyan]  Chandelier  [/bold bright_cyan]",
                 subtitle=subtitle,
                 border_style="bright_blue")


def build_header(state: dict) -> Panel:
    price = state.get("current_price") or state.get("grid_base_price") or 0
    base_price = state.get("grid_base_price") or 0
    drift = abs(price - base_price) / base_price * 100 if base_price else 0
    elapsed = format_elapsed(state.get("start_time", ""))
    grid_profit = state.get("total_profit", 0)
    trades = state.get("total_trades", 0)
    start_val = state.get("start_portfolio_value") or 1
    active = len(state.get("grid_orders", {}))
    portfolio = state.get("portfolio_value", 0)
    capital = state.get("effective_capital", 0)
    alloc = state.get("capital_allocation", 0)

    portfolio_pnl = portfolio - start_val
    portfolio_roi = (portfolio_pnl / start_val * 100) if start_val else 0

    pnl_color = "green" if portfolio_pnl >= 0 else "red"
    pnl_arrow = "▲" if portfolio_pnl >= 0 else "▼"
    grid_color = "green" if grid_profit >= 0 else "red"
    drift_style = "bold red" if drift > 5 else ("yellow" if drift > 3 else "green")

    now_str = datetime.now(TZ).strftime("%H:%M:%S")

    # Line 1: Symbol + Price
    h = Text()
    h.append(f"  ◈ {SYMBOL} ", style="bold bright_cyan")
    h.append(f"  ${price:,.2f}", style="bold white on grey23")
    h.append(f"   Base ${base_price:,.2f}", style="dim")
    h.append(f"   Drift ", style="dim")
    h.append(f"{drift:.2f}%", style=drift_style)
    h.append(f"          {now_str}", style="dim italic")

    # Line 2: PnL metrics
    h.append(f"\n  {pnl_arrow} PnL ", style=f"bold {pnl_color}")
    h.append(f"{portfolio_pnl:+,.2f} USDT ", style=f"bold {pnl_color}")
    h.append(f"({portfolio_roi:+.2f}%)", style=f"{pnl_color}")
    h.append(f"   Grid ", style="dim")
    h.append(f"{grid_profit:+.4f}", style=f"bold {grid_color}")
    h.append(f"   Trades ", style="dim")
    h.append(f"{trades}", style="bold bright_white")
    h.append(f"   Ordres ", style="dim")
    h.append(f"{active}", style="bold bright_white")
    h.append(f"   Uptime ", style="dim")
    h.append(f"{elapsed}", style="bright_white")

    # Line 3: Portfolio bar
    h.append(f"\n  Portefeuille ", style="dim")
    h.append(f"${portfolio:,.2f}", style="bold bright_white")
    h.append(f"  Capital ", style="dim")
    h.append(f"${capital:,.2f}", style="bold cyan")
    h.append("  [", style="dim")
    bar_len = 20
    filled = int(alloc / 100 * bar_len) if alloc else 0
    h.append("█" * filled, style="cyan")
    h.append("░" * (bar_len - filled), style="bright_black")
    h.append(f"] {alloc:.0f}%", style="dim")

    return Panel(h, title="[bold bright_white] GRID TRADING BOT [/bold bright_white]", border_style="bright_blue", subtitle=f"[dim]{STATE_FILE}[/dim]")


def build_grid_visual(state: dict) -> Panel:
    """Visual grid with horizontal bars showing order sizes."""
    grid_orders = state.get("grid_orders", {})
    price = state.get("current_price") or state.get("grid_base_price") or 0

    orders = list(grid_orders.values())
    orders.sort(key=lambda o: o["price"], reverse=True)

    if not orders:
        return Panel(Text("  Aucun ordre actif", style="dim italic"), title="[bold] Grille [/bold]", border_style="yellow")

    max_size = max(o["size"] for o in orders) if orders else 1
    bar_width = 15

    table = Table(show_header=True, header_style="bold", expand=True, padding=(0, 1), show_lines=False)
    table.add_column("", justify="center", width=3)
    table.add_column("Prix", justify="right", width=12)
    table.add_column("Taille", justify="right", width=12)
    table.add_column("Visualisation", justify="left", min_width=bar_width + 2)
    table.add_column("Dist.", justify="right", width=7)

    # Insert current price marker at correct position
    price_inserted = False
    for order in orders:
        o_price = order["price"]
        side = order["side"]
        size = order["size"]
        is_counter = order.get("is_counter", False)

        # Insert price marker if we've crossed it
        if not price_inserted and o_price < price:
            table.add_row(
                Text("◆", style="bold yellow"),
                Text(f"${price:,.2f}", style="bold yellow"),
                Text("PRIX ACTUEL", style="bold yellow"),
                Text("━" * (bar_width + 2), style="yellow"),
                "",
            )
            price_inserted = True

        # Order row
        if side == "sell":
            icon = "▼" if is_counter else "○"
            color = "bright_red" if is_counter else "red"
            bar_char = "▓"
        else:
            icon = "▲" if is_counter else "○"
            color = "bright_green" if is_counter else "green"
            bar_char = "▓"

        bar = make_bar(size, max_size, bar_width, color)
        dist = (o_price - price) / price * 100 if price else 0
        dist_style = "red" if dist > 0 else "green"

        table.add_row(
            Text(icon, style=f"bold {color}"),
            Text(f"${o_price:,.2f}", style=color),
            Text(f"{size:.6f}", style=f"dim {color}"),
            bar,
            Text(f"{dist:+.2f}%", style=dist_style),
        )

    # If price is below all orders
    if not price_inserted:
        table.add_row(
            Text("◆", style="bold yellow"),
            Text(f"${price:,.2f}", style="bold yellow"),
            Text("PRIX ACTUEL", style="bold yellow"),
            Text("━" * (bar_width + 2), style="yellow"),
            "",
        )

    n_buys = sum(1 for o in orders if o["side"] == "buy")
    n_sells = sum(1 for o in orders if o["side"] == "sell")
    n_counters = sum(1 for o in orders if o.get("is_counter"))
    subtitle = f"[dim]Spread {GRID_SPREAD*100:.2f}%  |  {n_sells} sells  {n_buys} buys  |  {n_counters} counters[/dim]"

    return Panel(table, title="[bold] Grille d'ordres [/bold]", subtitle=subtitle, border_style="yellow")


def build_fills_table(state: dict) -> Panel:
    fills = state.get("filled_orders", [])
    recent = fills[-20:] if fills else []
    recent.reverse()

    table = Table(show_header=True, header_style="bold bright_white", expand=True, padding=(0, 1), show_lines=False)
    table.add_column("", width=2)
    table.add_column("Heure", style="dim", justify="left", width=8)
    table.add_column("Side", justify="center", width=5)
    table.add_column("Prix", justify="right", width=12)
    table.add_column("Taille", justify="right", width=10)
    table.add_column("Profit", justify="right", width=10)

    for i, fill in enumerate(recent):
        side = fill.get("side", "?")
        profit = fill.get("profit", 0)
        is_counter = fill.get("is_counter", False)

        if side == "buy":
            icon = "▲"
            side_style = "bold green"
        else:
            icon = "▼"
            side_style = "bold red"

        if profit > 0:
            profit_str = f"+{profit:.4f}"
            profit_style = "bold green"
        elif profit < 0:
            profit_str = f"{profit:.4f}"
            profit_style = "bold red"
        else:
            profit_str = "─"
            profit_style = "dim"

        fill_time = fill.get("fill_time", "")
        try:
            t = datetime.fromisoformat(fill_time)
            time_str = t.strftime("%H:%M:%S")
        except Exception:
            time_str = "?"

        # Subtle alternating background
        row_style = "" if i % 2 == 0 else "on grey7"

        table.add_row(
            Text(icon, style=side_style),
            Text(time_str, style=f"dim {row_style}"),
            Text(side.upper(), style=f"{side_style} {row_style}"),
            Text(f"${fill.get('price', 0):,.2f}", style=row_style),
            Text(f"{fill.get('size', 0):.6f}", style=f"dim {row_style}"),
            Text(profit_str, style=f"{profit_style} {row_style}"),
        )

    if not recent:
        table.add_row("", "", "", Text("En attente...", style="dim italic"), "", "")

    total_profit = sum(f.get("profit", 0) for f in fills if f.get("profit", 0) > 0)
    profit_color = "green" if total_profit >= 0 else "red"
    subtitle = f"[dim]Total: {len(fills)} fills  |  Profit cumule: [{profit_color}]{total_profit:+.4f}[/{profit_color}][/dim]"
    return Panel(table, title="[bold] Derniers fills [/bold]", subtitle=subtitle, border_style="magenta")


def build_balance_panel(state: dict) -> Panel:
    balance = state.get("balance", {})
    price = state.get("current_price") or state.get("grid_base_price") or 0
    base_asset = SYMBOL.split("/")[0]
    quote_asset = SYMBOL.split("/")[1]

    quote = balance.get(quote_asset, {})
    q_free = quote.get("free", 0)
    q_used = quote.get("used", 0)
    q_total = quote.get("total", 0)

    base = balance.get(base_asset, {})
    b_free = base.get("free", 0)
    b_used = base.get("used", 0)
    b_total = base.get("total", 0)
    b_value = b_total * price if price else 0

    total_value = q_total + b_value

    # Build a compact visual balance
    t = Text()

    # Quote asset
    t.append(f"  {quote_asset:>5} ", style="bold cyan")
    q_bar_ratio = q_total / total_value if total_value else 0
    bar_w = 12
    filled = int(q_bar_ratio * bar_w)
    t.append("▐", style="dim")
    t.append("█" * filled, style="bright_cyan")
    t.append("░" * (bar_w - filled), style="bright_black")
    t.append("▌", style="dim")
    t.append(f" ${q_total:>10,.2f}", style="bold white")
    t.append(f"  (libre: {q_free:,.2f}  ordres: {q_used:,.2f})", style="dim")

    # Base asset
    t.append(f"\n  {base_asset:>5} ", style="bold cyan")
    b_bar_ratio = b_value / total_value if total_value else 0
    filled = int(b_bar_ratio * bar_w)
    t.append("▐", style="dim")
    t.append("█" * filled, style="bright_yellow")
    t.append("░" * (bar_w - filled), style="bright_black")
    t.append("▌", style="dim")
    t.append(f" ${b_value:>10,.2f}", style="bold white")
    t.append(f"  ({b_total:.6f} @ ${price:,.2f})", style="dim")

    # Total
    start_val = state.get("start_portfolio_value") or total_value
    pnl = total_value - start_val if total_value > 0 else 0
    pnl_color = "green" if pnl >= 0 else "red"
    pnl_icon = "▲" if pnl >= 0 else "▼"

    t.append(f"\n  {'TOTAL':>5} ", style="bold white")
    t.append("▐", style="dim")
    t.append("█" * bar_w, style="bright_white")
    t.append("▌", style="dim")
    t.append(f" ${total_value:>10,.2f}", style="bold bright_white")
    t.append(f"  {pnl_icon} ", style=f"bold {pnl_color}")
    t.append(f"{pnl:+,.2f} USDT", style=f"bold {pnl_color}")
    t.append(f" depuis ${start_val:,.2f}", style="dim")

    return Panel(t, title="[bold] Soldes [/bold]", border_style="green")


def build_indicators_panel(state: dict) -> Panel:
    """Panel showing all technical indicators with status lights."""
    ind = state.get("_indicators", {})
    price = state.get("current_price") or state.get("grid_base_price") or 0

    t = Text()

    # ── RSI ──
    rsi = ind.get("rsi", 50)
    rsi_str = ind.get("rsi_strength", 0)
    if rsi_str == 0:
        rsi_icon = "⊘"
        rsi_style = "dim"
        rsi_status = "OFF"
        rsi_status_style = "dim"
    elif rsi < 30:
        rsi_icon = "●"
        rsi_style = "bold bright_green"
        rsi_status = "SURVENDU"
        rsi_status_style = "bold green"
    elif rsi > 70:
        rsi_icon = "●"
        rsi_style = "bold bright_red"
        rsi_status = "SURACHETE"
        rsi_status_style = "bold red"
    elif rsi < 40:
        rsi_icon = "●"
        rsi_style = "bright_green"
        rsi_status = "Favorable"
        rsi_status_style = "green"
    elif rsi > 60:
        rsi_icon = "●"
        rsi_style = "bright_red"
        rsi_status = "Prudence"
        rsi_status_style = "red"
    else:
        rsi_icon = "●"
        rsi_style = "yellow"
        rsi_status = "Neutre"
        rsi_status_style = "yellow"

    t.append(f"  {rsi_icon}", style=rsi_style)
    t.append(f" RSI({ind.get('rsi_period', 14)})", style="bold white")
    t.append(f"  {rsi:.1f}  ", style="bright_white")
    # RSI gauge bar
    rsi_bar_w = 20
    rsi_pos = int(rsi / 100 * rsi_bar_w)
    t.append("[", style="dim")
    for i in range(rsi_bar_w):
        if i == rsi_pos:
            t.append("◆", style="bold bright_white")
        elif i < rsi_bar_w * 0.3:
            t.append("─", style="green")
        elif i > rsi_bar_w * 0.7:
            t.append("─", style="red")
        else:
            t.append("─", style="yellow")
    t.append("]", style="dim")
    t.append(f"  {rsi_status}", style=rsi_status_style)

    # ── EMA Trend ──
    ema_trend = ind.get("ema_trend", 0)
    ema_str = ind.get("ema_strength", 0)
    if ema_str == 0:
        ema_icon = "⊘"
        ema_style = "dim"
        ema_status = "OFF"
        ema_status_style = "dim"
    elif ema_trend > 0.002:
        ema_icon = "●"
        ema_style = "bold bright_green"
        ema_status = "HAUSSIER"
        ema_status_style = "bold green"
    elif ema_trend < -0.002:
        ema_icon = "●"
        ema_style = "bold bright_red"
        ema_status = "BAISSIER"
        ema_status_style = "bold red"
    elif ema_trend > 0:
        ema_icon = "●"
        ema_style = "bright_green"
        ema_status = "Legerement haussier"
        ema_status_style = "green"
    elif ema_trend < 0:
        ema_icon = "●"
        ema_style = "bright_red"
        ema_status = "Legerement baissier"
        ema_status_style = "red"
    else:
        ema_icon = "●"
        ema_style = "yellow"
        ema_status = "Neutre"
        ema_status_style = "yellow"

    fast = ind.get("ema_fast", 12)
    slow = ind.get("ema_slow", 26)
    t.append(f"\n  {ema_icon}", style=ema_style)
    t.append(f" EMA({fast}/{slow})", style="bold white")
    t.append(f"  {ema_trend:+.4f}  ", style="bright_white")
    # Trend arrow visualization
    trend_bar_w = 20
    t.append("[", style="dim")
    mid = trend_bar_w // 2
    norm_trend = max(-1, min(1, ema_trend / 0.005))  # normalize to -1..1
    pos = int((norm_trend + 1) / 2 * (trend_bar_w - 1))
    for i in range(trend_bar_w):
        if i == mid:
            if i == pos:
                t.append("◆", style="bold bright_white")
            else:
                t.append("│", style="dim")
        elif i == pos:
            t.append("◆", style="bold bright_white")
        elif i < mid:
            t.append("─", style="red dim")
        else:
            t.append("─", style="green dim")
    t.append("]", style="dim")
    t.append(f"  {ema_status}", style=ema_status_style)

    # ── Bollinger Bands ──
    bb_spread = ind.get("bb_spread", 0)
    bb_enabled = ind.get("bb_enabled", False)
    if not bb_enabled:
        bb_icon = "⊘"
        bb_style = "dim"
        bb_status = "OFF"
        bb_status_style = "dim"
    elif bb_spread > 0.02:
        bb_icon = "●"
        bb_style = "bold bright_red"
        bb_status = "LARGE (haute vol.)"
        bb_status_style = "bold red"
    elif bb_spread > 0.01:
        bb_icon = "●"
        bb_style = "yellow"
        bb_status = "Moyen"
        bb_status_style = "yellow"
    else:
        bb_icon = "●"
        bb_style = "bright_green"
        bb_status = "SERRE (basse vol.)"
        bb_status_style = "green"

    bb_period = ind.get("bb_period", 20)
    t.append(f"\n  {bb_icon}", style=bb_style)
    t.append(f" Bollinger({bb_period})", style="bold white")
    t.append(f"  {bb_spread*100:.2f}%  ", style="bright_white")
    t.append(f"{bb_status}", style=bb_status_style)

    # ── Fear & Greed ──
    fg = ind.get("fear_greed", 50)
    fg_enabled = ind.get("fg_enabled", False)
    if not fg_enabled:
        fg_icon = "⊘"
        fg_style = "dim"
        fg_status = "OFF"
        fg_status_style = "dim"
    elif fg < 25:
        fg_icon = "●"
        fg_style = "bold bright_green"
        fg_status = "PEUR EXTREME"
        fg_status_style = "bold green"
    elif fg < 40:
        fg_icon = "●"
        fg_style = "bright_green"
        fg_status = "Peur"
        fg_status_style = "green"
    elif fg > 75:
        fg_icon = "●"
        fg_style = "bold bright_red"
        fg_status = "CUPIDITE EXTREME"
        fg_status_style = "bold red"
    elif fg > 60:
        fg_icon = "●"
        fg_style = "bright_red"
        fg_status = "Cupidite"
        fg_status_style = "red"
    else:
        fg_icon = "●"
        fg_style = "yellow"
        fg_status = "Neutre"
        fg_status_style = "yellow"

    t.append(f"\n  {fg_icon}", style=fg_style)
    t.append(f" Fear&Greed", style="bold white")
    t.append(f"  {fg}  ", style="bright_white")
    # F&G gauge
    fg_bar_w = 20
    fg_pos = int(fg / 100 * (fg_bar_w - 1))
    t.append("[", style="dim")
    for i in range(fg_bar_w):
        if i == fg_pos:
            t.append("◆", style="bold bright_white")
        elif i < fg_bar_w * 0.25:
            t.append("─", style="green")
        elif i < fg_bar_w * 0.45:
            t.append("─", style="bright_green")
        elif i < fg_bar_w * 0.55:
            t.append("─", style="yellow")
        elif i < fg_bar_w * 0.75:
            t.append("─", style="bright_red")
        else:
            t.append("─", style="red")
    t.append("]", style="dim")
    t.append(f"  {fg_status}", style=fg_status_style)

    # ── Volatilite ──
    vol = ind.get("volatility", 0)
    if vol > 2:
        vol_icon = "●"
        vol_style = "bold bright_red"
        vol_status = "HAUTE"
        vol_status_style = "bold red"
    elif vol > 1:
        vol_icon = "●"
        vol_style = "yellow"
        vol_status = "Moyenne"
        vol_status_style = "yellow"
    elif vol > 0:
        vol_icon = "●"
        vol_style = "bright_green"
        vol_status = "Basse"
        vol_status_style = "green"
    else:
        vol_icon = "●"
        vol_style = "dim"
        vol_status = "N/A"
        vol_status_style = "dim"

    t.append(f"\n  {vol_icon}", style=vol_style)
    t.append(f" Volatilite 24h", style="bold white")
    t.append(f"  {vol:.2f}%  ", style="bright_white")
    t.append(f"{vol_status}", style=vol_status_style)

    # ── Inventory ratio ──
    inv = ind.get("inventory_ratio", 0.5)
    base_asset = SYMBOL.split("/")[0]
    if inv > 0.7:
        inv_icon = "●"
        inv_style = "bold bright_red"
        inv_status = f"Surcharge {base_asset}"
        inv_status_style = "bold red"
    elif inv < 0.3:
        inv_icon = "●"
        inv_style = "bold bright_cyan"
        inv_status = f"Sous-expose {base_asset}"
        inv_status_style = "bold cyan"
    else:
        inv_icon = "●"
        inv_style = "bright_green"
        inv_status = "Equilibre"
        inv_status_style = "green"

    t.append(f"\n  {inv_icon}", style=inv_style)
    t.append(f" Inventaire", style="bold white")
    t.append(f"  {inv*100:.0f}% {base_asset}  ", style="bright_white")
    inv_bar_w = 20
    inv_pos = int(inv * (inv_bar_w - 1))
    t.append("[", style="dim")
    for i in range(inv_bar_w):
        if i == inv_pos:
            t.append("◆", style="bold bright_white")
        elif i < inv_bar_w // 2:
            t.append("─", style="cyan")
        else:
            t.append("─", style="yellow")
    t.append("]", style="dim")
    t.append(f"  {inv_status}", style=inv_status_style)

    # ── Spread effectif ──
    spread = ind.get("spread", 0)
    base_spread = ind.get("base_spread", 0)
    grid_type = ind.get("grid_type", "?")
    grid_levels = ind.get("grid_levels", 0)

    spread_ratio = spread / base_spread if base_spread else 1
    if spread_ratio > 2:
        sp_style = "bold red"
    elif spread_ratio > 1.3:
        sp_style = "yellow"
    else:
        sp_style = "green"

    t.append(f"\n  ─────────────────────────────────", style="bright_black")
    t.append(f"\n  Spread effectif: ", style="dim")
    t.append(f"{spread*100:.3f}%", style=f"bold {sp_style}")
    t.append(f"  (base: {base_spread*100:.2f}%)", style="dim")
    t.append(f"  |  Grille: ", style="dim")
    t.append(f"{grid_type} x{grid_levels}", style="bright_white")

    # Count green/red indicators
    active_indicators = []
    if rsi_str > 0:
        active_indicators.append(("RSI", rsi_style))
    if ema_str > 0:
        active_indicators.append(("EMA", ema_style))
    if bb_enabled:
        active_indicators.append(("BB", bb_style))
    if fg_enabled:
        active_indicators.append(("F&G", fg_style))

    if active_indicators:
        n_green = sum(1 for _, s in active_indicators if "green" in s)
        n_red = sum(1 for _, s in active_indicators if "red" in s)
        n_neutral = len(active_indicators) - n_green - n_red
        t.append(f"\n  Signal: ", style="dim")
        if n_green > n_red:
            t.append(f"FAVORABLE ", style="bold bright_green")
        elif n_red > n_green:
            t.append(f"DEFAVORABLE ", style="bold bright_red")
        else:
            t.append(f"MIXTE ", style="bold yellow")
        t.append(f"({n_green}↑ {n_red}↓ {n_neutral}→)", style="dim")

    if not ind:
        t.append("\n  En attente des donnees du bot...", style="dim italic")

    return Panel(t, title="[bold] Indicateurs techniques [/bold]", border_style="bright_cyan")


def build_dashboard(state: dict) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=7),
        Layout(name="body"),
        Layout(name="footer", size=6),
    )
    layout["body"].split_row(
        Layout(name="main", ratio=3),
        Layout(name="sidebar", ratio=2),
    )
    layout["main"].split_column(
        Layout(name="chart"),
        Layout(name="bottom_left"),
    )
    layout["bottom_left"].split_column(
        Layout(name="grid"),
        Layout(name="indicators", size=14),
    )
    layout["sidebar"].split_column(
        Layout(name="fills"),
    )

    layout["header"].update(build_header(state))
    layout["chart"].update(build_price_chart(state))
    layout["grid"].update(build_grid_visual(state))
    layout["indicators"].update(build_indicators_panel(state))
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
