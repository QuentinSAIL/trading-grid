#!/usr/bin/env python3
"""
Grid Trading Bot — Strategie volatilite
Exchange : MEXC (maker 0%, taker 0.1%)
"""

import ccxt
import os
import math
import time
import json
import signal
import logging
import functools
from logging.handlers import RotatingFileHandler
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---------------------------------------------------------------

EXCHANGE_ID     = os.getenv("EXCHANGE", "mexc")
API_KEY         = os.getenv("API_KEY", "")
API_SECRET      = os.getenv("API_SECRET", "")
SYMBOL          = os.getenv("SYMBOL", "BTC/USDT")
CAPITAL_ALLOC   = float(os.getenv("CAPITAL_ALLOCATION", 90))  # % du portefeuille
MIN_CAPITAL     = float(os.getenv("MIN_CAPITAL", 30))         # minimum USDT pour trader
GRID_LEVELS     = int(os.getenv("GRID_LEVELS", 10))
GRID_SPREAD     = float(os.getenv("GRID_SPREAD", 0.005))
PRICE_RANGE_PCT = float(os.getenv("PRICE_RANGE_PCT", 0.03))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", 0.25))
MAX_OPEN_ORDERS = int(os.getenv("MAX_OPEN_ORDERS", 20))
MAKER_FEE       = float(os.getenv("MAKER_FEE", 0.0))    # 0% maker (MEXC)
TAKER_FEE       = float(os.getenv("TAKER_FEE", 0.001))   # 0.1% taker (MEXC)
MAX_FILLED_HISTORY = int(os.getenv("MAX_FILLED_HISTORY", 500))
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
PAPER_TRADING   = os.getenv("PAPER_TRADING", "true").lower() == "true"

DATA_DIR   = os.path.dirname(os.getenv("STATE_FILE", "/app/data/bot_state.json"))
STATE_FILE = os.getenv("STATE_FILE", "/app/data/bot_state.json")
LOG_FILE   = os.path.join(DATA_DIR, "bot.log")

os.makedirs(DATA_DIR, exist_ok=True)

# --- LOGGING ---------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# --- API RETRY -------------------------------------------------------------------

def api_retry(max_retries=3, base_delay=1.0):
    """Retry decorator for exchange API calls with exponential backoff.
    Only retries transient network errors, not business logic errors."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (ccxt.NetworkError, ccxt.ExchangeNotAvailable,
                        ccxt.RequestTimeout) as e:
                    last_exc = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        log.warning(f"API retry {attempt+1}/{max_retries} "
                                    f"for {func.__name__}: {e}")
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator

# --- STATE -----------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Impossible de charger l'etat: {e} — reinitialise")
    return {
        "grid_orders": {},
        "filled_orders": [],
        "total_profit": 0.0,
        "total_trades": 0,
        "start_time": datetime.now(TZ).isoformat(),
        "grid_base_price": None,
        "current_price": None,
        "balance": {},
        "portfolio_value": 0.0,
        "effective_capital": 0.0,
        "capital_allocation": CAPITAL_ALLOC,
        "start_portfolio_value": None,
    }

def save_state(state: dict):
    if len(state.get("filled_orders", [])) > MAX_FILLED_HISTORY:
        state["filled_orders"] = state["filled_orders"][-MAX_FILLED_HISTORY:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)

# --- DISCORD ---------------------------------------------------------------------

def notify(message: str, color: int = 0x00ff88):
    if not DISCORD_WEBHOOK:
        return
    try:
        payload = {
            "embeds": [{
                "description": message,
                "color": color,
                "footer": {"text": f"Grid Bot | {SYMBOL} "
                           f"| {'PAPER' if PAPER_TRADING else 'LIVE'}"},
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Discord notification failed: {e}")

# --- EXCHANGE --------------------------------------------------------------------

def init_exchange() -> ccxt.Exchange:
    exchange_class = getattr(ccxt, EXCHANGE_ID)
    exchange = exchange_class({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    exchange.load_markets()
    log.info(f"Connecte a {EXCHANGE_ID.upper()} — "
             f"{SYMBOL} disponible: {SYMBOL in exchange.markets}")
    return exchange

def get_market_info(exchange) -> dict:
    """Fetch market limits from exchange."""
    market = exchange.market(SYMBOL)
    return {
        "min_amount": float(market.get("limits", {})
                            .get("amount", {}).get("min") or 0),
        "min_cost": float(market.get("limits", {})
                          .get("cost", {}).get("min") or 0),
    }

# --- PRECISION -------------------------------------------------------------------

def round_price(exchange, price: float) -> float:
    """Round price using exchange precision rules (handles all precisionMode)."""
    return float(exchange.price_to_precision(SYMBOL, price))

def round_amount(exchange, amount: float) -> float:
    """Round/truncate amount using exchange precision rules."""
    return float(exchange.amount_to_precision(SYMBOL, amount))

# --- BALANCE ---------------------------------------------------------------------

@api_retry()
def fetch_balance(exchange, state: dict, price: float = None):
    """Recupere le solde, calcule la valeur du portefeuille et le capital effectif."""
    base_asset = SYMBOL.split("/")[0]
    quote_asset = SYMBOL.split("/")[1]
    balance = exchange.fetch_balance()

    q = {
        "free": float(balance.get(quote_asset, {}).get("free", 0) or 0),
        "used": float(balance.get(quote_asset, {}).get("used", 0) or 0),
        "total": float(balance.get(quote_asset, {}).get("total", 0) or 0),
    }
    b = {
        "free": float(balance.get(base_asset, {}).get("free", 0) or 0),
        "used": float(balance.get(base_asset, {}).get("used", 0) or 0),
        "total": float(balance.get(base_asset, {}).get("total", 0) or 0),
    }

    state["balance"] = {quote_asset: q, base_asset: b}

    p = price or state.get("current_price") or state.get("grid_base_price") or 0
    portfolio_value = q["total"] + b["total"] * p
    effective_capital = portfolio_value * CAPITAL_ALLOC / 100

    state["portfolio_value"] = round(portfolio_value, 4)
    state["effective_capital"] = round(effective_capital, 4)
    state["capital_allocation"] = CAPITAL_ALLOC

# --- PRIX & VOLATILITE ----------------------------------------------------------

@api_retry()
def get_current_price(exchange) -> float:
    ticker = exchange.fetch_ticker(SYMBOL)
    return float(ticker["last"])

def get_volatility(exchange) -> float:
    """Volatilite horaire sur 24h (ecart-type des rendements en %)."""
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, "1h", limit=25)
        closes = [c[4] for c in ohlcv]
        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                   for i in range(1, len(closes))]
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance) * 100
    except Exception as e:
        log.warning(f"Impossible de calculer la volatilite: {e}")
        return 0.0

# --- GRILLE ----------------------------------------------------------------------

def adapt_spread(exchange) -> float:
    """Ajuste le spread en fonction de la volatilite horaire."""
    vol = get_volatility(exchange)
    if vol <= 0:
        return GRID_SPREAD
    target = vol / 100 * 2.5  # 2.5x la volatilite horaire
    min_spread = GRID_SPREAD * 0.5
    max_spread = GRID_SPREAD * 4.0
    return max(min_spread, min(max_spread, target))


def compute_grid(base_price: float, exchange,
                 spread: float = None) -> list:
    """Genere les niveaux buy/sell autour du prix de base."""
    s = spread or GRID_SPREAD
    levels = []
    for i in range(1, GRID_LEVELS + 1):
        levels.append({
            "price": round_price(exchange, base_price * (1 - i * s)),
            "side": "buy",
            "index": i
        })
        levels.append({
            "price": round_price(exchange, base_price * (1 + i * s)),
            "side": "sell",
            "index": i
        })
    return levels

def inventory_ratio(state: dict, price: float) -> float:
    """Part du portefeuille en base asset (0=tout quote, 1=tout base)."""
    base_asset = SYMBOL.split("/")[0]
    quote_asset = SYMBOL.split("/")[1]
    balance = state.get("balance", {})
    quote_total = balance.get(quote_asset, {}).get("total", 0)
    base_total = balance.get(base_asset, {}).get("total", 0)
    portfolio = quote_total + base_total * price
    if portfolio <= 0:
        return 0.5
    return (base_total * price) / portfolio


def order_size(base_price: float, capital: float, exchange,
               side: str = "buy", inv_ratio: float = 0.5) -> float:
    """Taille d'ordre par niveau avec gestion d'inventaire."""
    base_size_usdt = (capital / 2) / GRID_LEVELS

    if side == "buy":
        if inv_ratio > 0.5:
            factor = max(0.2, 1.0 - (inv_ratio - 0.5) * 3)
            base_size_usdt *= factor
    else:
        if inv_ratio > 0.5:
            factor = min(1.8, 1.0 + (inv_ratio - 0.5) * 2)
            base_size_usdt *= factor
        elif inv_ratio < 0.2:
            factor = max(0.3, inv_ratio / 0.4)
            base_size_usdt *= factor

    return round_amount(exchange, base_size_usdt / base_price)

# --- CANCEL ALL ORDERS -----------------------------------------------------------

def cancel_all_orders(exchange, state: dict) -> int:
    """Cancel all open orders on the exchange and clear grid state."""
    cancelled = 0
    # Cancel via exchange open orders (source of truth)
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)
        for o in open_orders:
            try:
                exchange.cancel_order(o["id"], SYMBOL)
                cancelled += 1
            except Exception as e:
                log.warning(f"Erreur annulation ordre {o['id']}: {e}")
    except Exception as e:
        log.warning(f"Erreur fetch open orders pour annulation: {e}")
        # Fallback: cancel from tracked state
        for oid in list(state.get("grid_orders", {}).keys()):
            try:
                exchange.cancel_order(oid, SYMBOL)
                cancelled += 1
            except Exception:
                pass

    state["grid_orders"] = {}
    if cancelled:
        log.info(f"{cancelled} ordres annules")
    return cancelled

# --- PROCESS FILL ----------------------------------------------------------------

def _process_fill(exchange, state: dict, oid: str, order: dict,
                  order_info: dict, market_info: dict):
    """Process a confirmed filled order: record profit and place counter-order."""
    fill_price = float(order_info.get("average", 0)
                       or order_info.get("price", 0)
                       or order["price"])
    filled_size = float(order_info.get("filled", 0) or order["size"])
    side = order["side"]
    is_counter = order.get("is_counter", False)

    # Frais: ordres limit = maker
    current_fee = fill_price * filled_size * MAKER_FEE
    profit = -current_fee

    if is_counter:
        original_price = order.get("original_fill_price")
        if original_price:
            # Profit exact: difference de prix reelle
            if side == "sell":   # counter sell apres buy initial
                gross = (fill_price - original_price) * filled_size
            else:                # counter buy apres sell initial
                gross = (original_price - fill_price) * filled_size
            # Frais: leg initiale (au prix d'origine) + leg actuelle
            initial_fee = original_price * filled_size * MAKER_FEE
        else:
            # Fallback approximation (anciens ordres sans original_fill_price)
            gross = fill_price * GRID_SPREAD * filled_size
            initial_fee = current_fee
        profit = gross - initial_fee - current_fee

    state["total_profit"] += profit
    state["total_trades"] += 1

    fill_record = {
        **order,
        "fill_time": datetime.now(TZ).isoformat(),
        "fill_price": fill_price,
        "filled_size": filled_size,
        "profit": profit,
    }
    state["filled_orders"].append(fill_record)

    if is_counter:
        total_fees = initial_fee + current_fee
        log.info(f"FILL {side.upper()} @ {fill_price:.2f} | "
                 f"+{profit:.4f} USDT net "
                 f"(cycle complete, frais: {total_fees:.4f})")
    else:
        log.info(f"FILL {side.upper()} @ {fill_price:.2f} | "
                 f"ordre initial (frais: {current_fee:.4f})")

    # Placer le contre-ordre
    try:
        counter_size = round_amount(exchange, filled_size)
        min_amount = market_info.get("min_amount", 0)
        min_cost = market_info.get("min_cost", 0)

        if counter_size < min_amount:
            log.warning(f"Fill trop petit pour contre-ordre: {counter_size} "
                        f"< min {min_amount}")
            return
        if side == "buy":
            counter_price = round_price(exchange,
                                        fill_price * (1 + GRID_SPREAD))
            if counter_size * counter_price < min_cost:
                log.warning(f"Contre-ordre sous cout min: "
                            f"{counter_size * counter_price:.2f} < {min_cost}")
                return
            new_order = exchange.create_limit_sell_order(
                SYMBOL, counter_size, counter_price)
        else:
            counter_price = round_price(exchange,
                                        fill_price * (1 - GRID_SPREAD))
            if counter_size * counter_price < min_cost:
                log.warning(f"Contre-ordre sous cout min: "
                            f"{counter_size * counter_price:.2f} < {min_cost}")
                return
            new_order = exchange.create_limit_buy_order(
                SYMBOL, counter_size, counter_price)

        new_oid = str(new_order.get("id", f"counter_{oid}"))
        state["grid_orders"][new_oid] = {
            "id": new_oid,
            "side": "sell" if side == "buy" else "buy",
            "price": counter_price,
            "size": counter_size,
            "index": order.get("index", 0),
            "placed_at": datetime.now(TZ).isoformat(),
            "is_counter": True,
            "original_fill_price": fill_price,
        }
        log.info(f"  Contre-ordre "
                 f"{'SELL' if side == 'buy' else 'BUY'} @ {counter_price:.2f}")
    except Exception as e:
        log.warning(f"  Erreur contre-ordre: {e}")

# --- PLACEMENT DE LA GRILLE -----------------------------------------------------

def place_grid(exchange, state: dict, market_info: dict):
    """Annule les ordres existants et place une nouvelle grille.
    Respecte les soldes disponibles: pas de sell sans actif."""
    cancel_all_orders(exchange, state)

    price = get_current_price(exchange)
    state["grid_base_price"] = price

    fetch_balance(exchange, state, price)
    capital = state.get("effective_capital", 0)
    if capital < MIN_CAPITAL:
        msg = (f"Capital insuffisant: {capital:.2f} USDT "
               f"(minimum: {MIN_CAPITAL:.0f} USDT) — bot arrete")
        log.error(msg)
        notify(msg, color=0xff0000)
        raise SystemExit(msg)

    base_asset = SYMBOL.split("/")[0]
    quote_asset = SYMBOL.split("/")[1]
    balance = state.get("balance", {})
    available_quote = balance.get(quote_asset, {}).get("free", 0)
    available_base = balance.get(base_asset, {}).get("free", 0)

    # Spread dynamique via volatilite
    current_spread = adapt_spread(exchange)

    inv_r = inventory_ratio(state, price)
    buy_size = order_size(price, capital, exchange, "buy", inv_r)
    sell_size = order_size(price, capital, exchange, "sell", inv_r)
    min_amount = market_info.get("min_amount", 0)
    min_cost = market_info.get("min_cost", 0)

    if buy_size < min_amount or (buy_size * price) < min_cost:
        msg = (f"Taille d'ordre trop petite: {buy_size} "
               f"(min amount: {min_amount}, min cost: {min_cost} USDT) "
               f"— augmenter le capital")
        log.error(msg)
        notify(msg, color=0xff0000)
        raise SystemExit(msg)

    grid = compute_grid(price, exchange, current_spread)

    log.info(f"Grille @ {price:.2f} | Capital: {capital:.2f} USDT "
             f"({CAPITAL_ALLOC:.0f}% de {state.get('portfolio_value', 0):.2f}) "
             f"| spread: {current_spread*100:.2f}% | inv: {inv_r*100:.0f}%")
    log.info(f"Solde libre: {available_quote:.2f} {quote_asset} | "
             f"{available_base:.6f} {base_asset}")

    placed = {}
    errors = 0
    quote_used = 0.0
    base_used = 0.0

    for level in grid:
        if len(placed) >= MAX_OPEN_ORDERS:
            log.warning(f"Limite MAX_OPEN_ORDERS ({MAX_OPEN_ORDERS}) atteinte")
            break
        try:
            size = buy_size if level["side"] == "buy" else sell_size
            if level["side"] == "buy":
                cost = size * level["price"]
                if quote_used + cost > available_quote:
                    log.debug(f"Skip buy @ {level['price']:.2f}: "
                              f"solde {quote_asset} insuffisant")
                    continue
                order = exchange.create_limit_buy_order(
                    SYMBOL, size, level["price"])
                quote_used += cost
            else:
                if base_used + size > available_base:
                    log.debug(f"Skip sell @ {level['price']:.2f}: "
                              f"solde {base_asset} insuffisant")
                    continue
                order = exchange.create_limit_sell_order(
                    SYMBOL, size, level["price"])
                base_used += size

            oid = str(order.get("id", f"grid_{level['side']}_{level['index']}"))
            placed[oid] = {
                "id": oid,
                "side": level["side"],
                "price": level["price"],
                "size": size,
                "index": level["index"],
                "placed_at": datetime.now(TZ).isoformat(),
                "is_counter": False,
            }
            log.info(f"  {level['side'].upper()} @ {level['price']:.2f}")
        except ccxt.InsufficientFunds as e:
            log.warning(f"  Solde insuffisant {level['side']} "
                        f"@ {level['price']:.2f}: {e}")
        except Exception as e:
            log.warning(f"  Erreur {level['side']} "
                        f"@ {level['price']:.2f}: {e}")
            errors += 1

    buys = sum(1 for o in placed.values() if o["side"] == "buy")
    sells = sum(1 for o in placed.values() if o["side"] == "sell")
    state["grid_orders"] = placed
    save_state(state)

    notify(
        f"**Grille placee — {SYMBOL}**\n"
        f"Prix: `{price:.2f}` | Niveaux: `{buys}B/{sells}S`\n"
        f"Taille/niveau: `{size}` | Capital: `{capital:.2f} USDT` "
        f"({CAPITAL_ALLOC:.0f}%)\n"
        f"Ordres places: `{len(placed)}` | Erreurs: `{errors}`"
    )

# --- REBALANCE -------------------------------------------------------------------

def rebalance_grid(exchange, state: dict, new_price: float, market_info: dict):
    """Recentre la grille. Preserve les contre-ordres, gere les fills partiels
    sur les ordres initiaux avant de les annuler."""
    grid_orders = state.get("grid_orders", {})
    counter_orders = {}
    initial_orders = {}

    for oid, o in grid_orders.items():
        if o.get("is_counter"):
            counter_orders[oid] = o
        else:
            initial_orders[oid] = o

    # Annuler les ordres initiaux D'ABORD, puis verifier les fills.
    # Cela evite la race condition: si un ordre se remplit entre
    # fetch_order et cancel_order, le fill serait perdu.
    for oid, order in initial_orders.items():
        try:
            # Annuler d'abord — si deja rempli, cancel echoue (OK)
            exchange.cancel_order(oid, SYMBOL)
        except Exception:
            pass

        # Maintenant verifier le status final (apres cancel)
        try:
            order_info = exchange.fetch_order(oid, SYMBOL)
            filled = float(order_info.get("filled", 0) or 0)
            status = order_info.get("status", "")

            if status == "closed":
                # Entierement rempli (avant ou malgre le cancel)
                _process_fill(exchange, state, oid, order, order_info,
                              market_info)
                continue

            # Partiellement rempli puis annule
            min_amount = market_info.get("min_amount", 0)
            if filled > 0 and round_amount(exchange, filled) >= min_amount:
                fill_price = float(order_info.get("average", 0)
                                   or order["price"])
                filled_rounded = round_amount(exchange, filled)
                side = order["side"]

                if side == "buy":
                    cp = round_price(exchange,
                                     fill_price * (1 + GRID_SPREAD))
                    counter = exchange.create_limit_sell_order(
                        SYMBOL, filled_rounded, cp)
                else:
                    cp = round_price(exchange,
                                     fill_price * (1 - GRID_SPREAD))
                    counter = exchange.create_limit_buy_order(
                        SYMBOL, filled_rounded, cp)

                new_oid = str(counter.get("id"))
                counter_orders[new_oid] = {
                    "id": new_oid,
                    "side": "sell" if side == "buy" else "buy",
                    "price": cp,
                    "size": filled_rounded,
                    "index": order.get("index", 0),
                    "placed_at": datetime.now(TZ).isoformat(),
                    "is_counter": True,
                    "original_fill_price": fill_price,
                }
                log.info(f"  Contre-ordre fill partiel: "
                         f"{'SELL' if side == 'buy' else 'BUY'} @ {cp:.2f} "
                         f"({filled_rounded})")
        except Exception as e:
            log.warning(f"Erreur verification ordre {oid} "
                        f"pendant rebalance: {e}")

    log.info(f"Rebalance: {len(initial_orders)} ordres initiaux traites, "
             f"{len(counter_orders)} contre-ordres preserves")

    # Recalculer le capital
    state["grid_base_price"] = new_price
    fetch_balance(exchange, state, new_price)
    capital = state.get("effective_capital", 0)
    if capital < MIN_CAPITAL:
        msg = (f"Capital insuffisant pour rebalance: {capital:.2f} USDT "
               f"— bot arrete")
        log.error(msg)
        notify(msg, color=0xff0000)
        raise SystemExit(msg)

    base_asset = SYMBOL.split("/")[0]
    quote_asset = SYMBOL.split("/")[1]
    balance = state.get("balance", {})
    available_quote = balance.get(quote_asset, {}).get("free", 0)
    available_base = balance.get(base_asset, {}).get("free", 0)

    current_spread = adapt_spread(exchange)
    inv_r = inventory_ratio(state, new_price)
    buy_size = order_size(new_price, capital, exchange, "buy", inv_r)
    sell_size = order_size(new_price, capital, exchange, "sell", inv_r)
    grid = compute_grid(new_price, exchange, current_spread)

    new_orders = dict(counter_orders)
    errors = 0
    quote_used = 0.0
    base_used = 0.0

    for level in grid:
        if len(new_orders) >= MAX_OPEN_ORDERS:
            break
        try:
            size = buy_size if level["side"] == "buy" else sell_size
            if level["side"] == "buy":
                cost = size * level["price"]
                if quote_used + cost > available_quote:
                    continue
                order = exchange.create_limit_buy_order(
                    SYMBOL, size, level["price"])
                quote_used += cost
            else:
                if base_used + size > available_base:
                    continue
                order = exchange.create_limit_sell_order(
                    SYMBOL, size, level["price"])
                base_used += size

            oid = str(order.get("id"))
            new_orders[oid] = {
                "id": oid,
                "side": level["side"],
                "price": level["price"],
                "size": size,
                "index": level["index"],
                "placed_at": datetime.now(TZ).isoformat(),
                "is_counter": False,
            }
        except ccxt.InsufficientFunds:
            pass
        except Exception as e:
            log.warning(f"  Erreur {level['side']} "
                        f"@ {level['price']:.2f}: {e}")
            errors += 1

    state["grid_orders"] = new_orders
    save_state(state)
    log.info(f"Rebalance terminee @ {new_price:.2f} | "
             f"{len(new_orders)} ordres actifs "
             f"(dont {len(counter_orders)} contre-ordres)")

# --- DETECTION DES FILLS ---------------------------------------------------------

def check_fills_live(exchange, state: dict, market_info: dict):
    """Detection robuste des fills: verifie le status via fetch_order.
    Ne suppose JAMAIS qu'un ordre disparu = rempli."""
    try:
        open_order_ids = {str(o["id"])
                          for o in exchange.fetch_open_orders(SYMBOL)}
    except Exception as e:
        log.warning(f"Erreur fetch_open_orders: {e}")
        return

    grid_orders = state.get("grid_orders", {})
    missing_ids = [oid for oid in grid_orders if oid not in open_order_ids]

    for oid in missing_ids:
        # Verification OBLIGATOIRE — pas de fill presume
        try:
            order_info = exchange.fetch_order(oid, SYMBOL)
        except Exception as e:
            log.warning(f"Impossible de verifier ordre {oid}: {e} "
                        f"— garde pour prochain cycle")
            continue

        status = order_info.get("status", "")

        if status in ("canceled", "cancelled", "expired", "rejected"):
            log.warning(f"Ordre {oid} {status} — retire du state")
            grid_orders.pop(oid, None)
            continue

        if status != "closed":
            log.warning(f"Ordre {oid} status inattendu '{status}' "
                        f"— garde pour verification")
            continue

        # Fill confirme — pop et traiter.
        # Si _process_fill echoue, le fill est quand meme enregistre
        # dans state (profit + filled_orders) mais le contre-ordre
        # pourra manquer. On ne remet PAS l'ordre dans grid_orders
        # car il est confirme rempli sur l'exchange.
        order = grid_orders.pop(oid)
        try:
            _process_fill(exchange, state, oid, order, order_info,
                          market_info)
        except Exception as e:
            log.error(f"Erreur traitement fill {oid}: {e} "
                      f"— fill enregistre mais contre-ordre manquant")

    state["grid_orders"] = grid_orders

# --- ORPHAN RECOVERY ON RESTART -------------------------------------------------

def recover_orphans(exchange, state: dict, market_info: dict) -> bool:
    """Au redemarrage, verifie les ordres orphelins.
    Si remplis, traite les fills (au lieu de les ignorer).
    Retourne True si la grille est encore valide, False si remplacement necessaire."""
    try:
        open_ids = {str(o["id"]) for o in exchange.fetch_open_orders(SYMBOL)}
    except Exception as e:
        log.warning(f"Impossible de verifier les ordres existants: {e}")
        return False

    grid_orders = state.get("grid_orders", {})
    orphans = [oid for oid in grid_orders if oid not in open_ids]

    if not orphans:
        return True

    log.info(f"{len(orphans)} ordres orphelins detectes — "
             f"verification des fills")

    for oid in orphans:
        order = grid_orders[oid]
        try:
            order_info = exchange.fetch_order(oid, SYMBOL)
            status = order_info.get("status", "")

            if status == "closed":
                log.info(f"Orphelin {oid} rempli — traitement du fill")
                grid_orders.pop(oid)
                _process_fill(exchange, state, oid, order, order_info,
                              market_info)
            elif status in ("canceled", "cancelled", "expired"):
                log.info(f"Orphelin {oid} annule — retire")
                grid_orders.pop(oid)
            else:
                log.warning(f"Orphelin {oid} status '{status}' "
                            f"— tentative d'annulation")
                try:
                    exchange.cancel_order(oid, SYMBOL)
                except Exception:
                    pass
                grid_orders.pop(oid)
        except Exception as e:
            log.warning(f"Impossible de verifier orphelin {oid}: {e} "
                        f"— retire par securite")
            grid_orders.pop(oid)

    state["grid_orders"] = grid_orders
    save_state(state)

    if not state["grid_orders"]:
        log.info("Aucun ordre valide restant — remplacement necessaire")
        return False

    return True

# --- PAPER TRADING ---------------------------------------------------------------

PAPER_BALANCE = float(os.getenv("PAPER_BALANCE", 100))

class PaperExchange:
    """Simulateur d'exchange pour paper trading — prix reels du marche.
    Valide les soldes et tracke les fills pour fetch_order."""

    def __init__(self):
        self._real_exchange = getattr(ccxt, EXCHANGE_ID)(
            {"enableRateLimit": True})
        self._real_exchange.load_markets()
        self.markets = self._real_exchange.markets
        self.orders: dict = {}
        self._filled_orders: dict = {}
        self._order_id = 1000
        self._usdt = PAPER_BALANCE
        self._btc = 0.0
        self._last_price = 0.0

    def _new_id(self) -> str:
        self._order_id += 1
        return f"paper_{self._order_id}"

    def load_markets(self):
        return self._real_exchange.markets

    def market(self, symbol: str) -> dict:
        return self._real_exchange.market(symbol)

    def price_to_precision(self, symbol: str, price) -> str:
        return self._real_exchange.price_to_precision(symbol, price)

    def amount_to_precision(self, symbol: str, amount) -> str:
        return self._real_exchange.amount_to_precision(symbol, amount)

    def fetch_ticker(self, symbol: str) -> dict:
        ticker = self._real_exchange.fetch_ticker(symbol)
        self._last_price = float(ticker["last"])
        return ticker

    def fetch_ohlcv(self, symbol: str, timeframe: str,
                    limit: int = 24) -> list:
        return self._real_exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    def _create_order(self, side: str, symbol: str, amount: float,
                      price: float) -> dict:
        # Valider le solde AVANT de creer l'ordre
        base_asset = SYMBOL.split("/")[0]
        if side == "buy":
            cost = amount * price
            used_usdt = sum(
                o["amount"] * o["price"]
                for o in self.orders.values() if o["side"] == "buy")
            available = self._usdt - used_usdt
            if cost > available:
                raise ccxt.InsufficientFunds(
                    f"Solde {SYMBOL.split('/')[1]} insuffisant: "
                    f"besoin {cost:.2f}, disponible {available:.2f}")
        else:
            used_btc = sum(
                o["amount"]
                for o in self.orders.values() if o["side"] == "sell")
            available = self._btc - used_btc
            if amount > available:
                raise ccxt.InsufficientFunds(
                    f"Solde {base_asset} insuffisant: "
                    f"besoin {amount:.6f}, disponible {available:.6f}")

        oid = self._new_id()
        self.orders[oid] = {
            "id": oid, "side": side, "amount": amount,
            "price": price, "status": "open", "symbol": symbol,
            "filled": 0, "average": 0, "remaining": amount,
        }
        return self.orders[oid]

    def create_limit_buy_order(self, symbol: str, amount: float,
                               price: float) -> dict:
        return self._create_order("buy", symbol, amount, price)

    def create_limit_sell_order(self, symbol: str, amount: float,
                                price: float) -> dict:
        return self._create_order("sell", symbol, amount, price)

    def fetch_open_orders(self, symbol: str) -> list:
        self._simulate_fills()
        return list(self.orders.values())

    def cancel_order(self, oid: str, symbol: str):
        if oid in self.orders:
            del self.orders[oid]

    def fetch_order(self, oid: str, symbol: str) -> dict:
        if oid in self.orders:
            return self.orders[oid]
        if oid in self._filled_orders:
            return self._filled_orders[oid]
        # Ordre inconnu = presume annule (pas rempli!)
        return {"id": oid, "status": "canceled", "filled": 0, "average": 0}

    def fetch_balance(self):
        base_asset = SYMBOL.split("/")[0]
        quote_asset = SYMBOL.split("/")[1]
        used_usdt = sum(
            o["amount"] * o["price"]
            for o in self.orders.values() if o["side"] == "buy")
        used_btc = sum(
            o["amount"]
            for o in self.orders.values() if o["side"] == "sell")
        return {
            quote_asset: {
                "free": max(0.0, self._usdt - used_usdt),
                "used": used_usdt,
                "total": self._usdt,
            },
            base_asset: {
                "free": max(0.0, self._btc - used_btc),
                "used": used_btc,
                "total": self._btc,
            },
        }

    def _simulate_fills(self):
        """Simule les fills quand le vrai prix croise un niveau.
        Les frais ne sont PAS deduits du solde ici — ils sont deja
        comptabilises dans _process_fill via le calcul de profit.
        Deduire les frais ici causerait un double-comptage et des
        erreurs InsufficientFunds sur les contre-ordres."""
        filled_ids = [
            oid for oid, o in self.orders.items()
            if (o["side"] == "buy" and self._last_price <= o["price"])
            or (o["side"] == "sell" and self._last_price >= o["price"])
        ]
        for oid in filled_ids:
            o = self.orders.pop(oid)
            if o["side"] == "buy":
                self._usdt -= o["amount"] * o["price"]
                self._btc += o["amount"]
            else:
                self._btc -= o["amount"]
                self._usdt += o["amount"] * o["price"]
            # Stocker pour verification via fetch_order
            self._filled_orders[oid] = {
                "id": oid, "status": "closed",
                "filled": o["amount"], "average": o["price"],
                "remaining": 0, "side": o["side"], "price": o["price"],
            }

# --- PORTFOLIO STOP LOSS ---------------------------------------------------------

def portfolio_stop_loss_triggered(state: dict) -> bool:
    """Verifie si la valeur du portefeuille a chute sous le seuil de stop loss.
    Base sur la VRAIE valeur du portefeuille, pas juste le profit tracke."""
    start_val = state.get("start_portfolio_value")
    current_val = state.get("portfolio_value")
    if start_val is None or current_val is None or start_val <= 0:
        return False
    loss_pct = (start_val - current_val) / start_val
    return loss_pct >= STOP_LOSS_PCT

# --- STATUS REPORT ---------------------------------------------------------------

def status_report(state: dict) -> str:
    elapsed = "N/A"
    try:
        start = datetime.fromisoformat(state["start_time"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        delta = datetime.now(TZ) - start
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        elapsed = f"{h}h{m:02d}m"
    except Exception:
        pass

    start_val = state.get("start_portfolio_value") or 1
    current_val = state.get("portfolio_value", 0)
    portfolio_pnl = current_val - start_val
    portfolio_roi = (portfolio_pnl / start_val * 100) if start_val else 0
    capital = state.get("effective_capital", 0)

    return (
        f"**Rapport Grid Bot**\n"
        f"Duree: `{elapsed}` | Trades: `{state['total_trades']}`\n"
        f"Profit grid: `{state['total_profit']:.4f} USDT`\n"
        f"Portefeuille: `{current_val:.2f} USDT` | "
        f"PnL: `{portfolio_pnl:+.2f} USDT` (`{portfolio_roi:+.2f}%`)\n"
        f"Capital alloue: `{capital:.2f} USDT` ({CAPITAL_ALLOC:.0f}%)\n"
        f"Ordres actifs: `{len(state.get('grid_orders', {}))}`"
    )

# --- BOUCLE PRINCIPALE -----------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Grid Trading Bot — demarrage")
    log.info(f"   Exchange : {EXCHANGE_ID.upper()}")
    log.info(f"   Paire    : {SYMBOL}")
    log.info(f"   Allocation: {CAPITAL_ALLOC}% du portefeuille")
    log.info(f"   Mode     : {'PAPER TRADING' if PAPER_TRADING else 'LIVE'}")
    log.info("=" * 60)

    state = load_state()

    if PAPER_TRADING:
        exchange = PaperExchange()
        log.info("Paper exchange initialise (prix reels via API publique)")
    else:
        try:
            exchange = init_exchange()
        except Exception as e:
            log.error(f"Impossible de se connecter a {EXCHANGE_ID}: {e}")
            notify(f"**Erreur de connexion**\n`{e}`", color=0xff0000)
            raise

    # Precision et limites du marche
    market_info = get_market_info(exchange)
    log.info(f"Limites marche: min amount={market_info['min_amount']}, "
             f"min cost={market_info['min_cost']} USDT")

    # Shutdown propre: annuler tous les ordres avant de quitter
    _shutdown_requested = False

    def clean_shutdown(reason: str):
        log.info(f"Arret propre: {reason}")
        log.info("Annulation de tous les ordres ouverts...")
        cancel_all_orders(exchange, state)
        save_state(state)
        report = status_report(state)
        log.info(report.replace("**", "").replace("`", ""))
        notify(report + f"\n\nBot arrete: {reason}")

    def sigterm_handler(signum, frame):
        nonlocal _shutdown_requested
        _shutdown_requested = True
        log.info("SIGTERM recu — arret demande au prochain cycle")

    signal.signal(signal.SIGTERM, sigterm_handler)

    # Snapshot initial du portefeuille
    init_price = get_current_price(exchange)
    fetch_balance(exchange, state, init_price)
    state["current_price"] = init_price
    if not state.get("start_portfolio_value"):
        state["start_portfolio_value"] = state.get("portfolio_value", 0)
    log.info(f"Portefeuille: {state['portfolio_value']:.2f} USDT | "
             f"Capital alloue: {state['effective_capital']:.2f} USDT "
             f"({CAPITAL_ALLOC:.0f}%)")

    if state["effective_capital"] < MIN_CAPITAL:
        msg = (f"Capital insuffisant: {state['effective_capital']:.2f} USDT "
               f"< {MIN_CAPITAL:.0f} USDT — bot arrete")
        log.error(msg)
        notify(msg, color=0xff0000)
        raise SystemExit(msg)

    vol = get_volatility(exchange)
    log.info(f"Volatilite 1h/24h: {vol:.3f}%")

    # Reprise ou placement initial
    if state.get("grid_orders") and state.get("grid_base_price"):
        log.info(f"Reprise grille existante "
                 f"({len(state['grid_orders'])} ordres, "
                 f"base: {state['grid_base_price']:.2f})")
        if not recover_orphans(exchange, state, market_info):
            place_grid(exchange, state, market_info)
    else:
        place_grid(exchange, state, market_info)

    notify("**Grid Bot demarre**\nSurveillance toutes les 30s.")
    last_report_hour = None

    while True:
        try:
            # Sleep fractionne pour reagir rapidement au SIGTERM
            # (Docker envoie SIGKILL apres stop_grace_period)
            for _ in range(6):
                time.sleep(5)
                if _shutdown_requested:
                    break

            if _shutdown_requested:
                clean_shutdown("SIGTERM recu")
                break

            price = get_current_price(exchange)
            state["current_price"] = price

            # Detecter les fills et placer les contre-ordres
            check_fills_live(exchange, state, market_info)

            # Mise a jour du solde (AVANT le stop loss)
            fetch_balance(exchange, state, price)

            # Stop loss base sur la valeur REELLE du portefeuille
            if portfolio_stop_loss_triggered(state):
                start_val = state.get("start_portfolio_value", 0)
                current_val = state.get("portfolio_value", 0)
                loss = start_val - current_val
                loss_pct = (loss / start_val * 100) if start_val else 0
                msg = (
                    f"**STOP LOSS DECLENCHE**\n"
                    f"Portefeuille: `{current_val:.2f}` "
                    f"(depart: `{start_val:.2f}`)\n"
                    f"Perte: `{loss:.2f} USDT` ({loss_pct:.1f}%)\n"
                    f"Seuil: {STOP_LOSS_PCT*100:.0f}%"
                )
                log.error(msg.replace("**", "").replace("`", ""))
                notify(msg, color=0xff0000)
                clean_shutdown("stop loss")
                raise SystemExit(msg)

            # Trailing grid base + recentrage
            base = state.get("grid_base_price") or price
            drift = abs(price - base) / base

            # Micro-ajustement: glisse la base de 10% vers le prix
            if drift > 0.005:
                state["grid_base_price"] = base * 0.9 + price * 0.1

            # Recentrage complet si drift trop important
            if drift > PRICE_RANGE_PCT:
                log.info(f"Drift {drift*100:.1f}% > "
                         f"{PRICE_RANGE_PCT*100:.0f}% — recentrage")
                rebalance_grid(exchange, state, price, market_info)

            # Rapport Discord a 8h et 20h (heure de Paris)
            now = datetime.now(TZ)
            if now.hour in (8, 20) and last_report_hour != now.hour:
                last_report_hour = now.hour
                report = status_report(state)
                log.info(report.replace("**", "").replace("`", ""))
                notify(report)
            elif now.hour not in (8, 20):
                last_report_hour = None

            save_state(state)

        except KeyboardInterrupt:
            clean_shutdown("arret manuel (Ctrl+C)")
            break
        except SystemExit:
            raise
        except Exception as e:
            log.error(f"Erreur inattendue: {e}", exc_info=True)
            notify(f"**Erreur**\n`{e}`\nReprise dans 60s.", color=0xff8800)
            time.sleep(60)


if __name__ == "__main__":
    main()
