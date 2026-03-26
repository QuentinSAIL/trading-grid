#!/usr/bin/env python3
"""
Grid Trading Bot — Stratégie volatilité
Exchange : MEXC (maker 0%, taker 0.1%)
"""

import ccxt
import os
import math
import time
import json
import logging
from logging.handlers import RotatingFileHandler
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

EXCHANGE_ID     = os.getenv("EXCHANGE", "mexc")
API_KEY         = os.getenv("API_KEY", "")
API_SECRET      = os.getenv("API_SECRET", "")
SYMBOL          = os.getenv("SYMBOL", "BTC/USDT")
CAPITAL_ALLOC   = float(os.getenv("CAPITAL_ALLOCATION", 90))  # % du portefeuille
MIN_CAPITAL     = float(os.getenv("MIN_CAPITAL", 30))        # minimum USDT pour trader
GRID_LEVELS     = int(os.getenv("GRID_LEVELS", 10))
GRID_SPREAD     = float(os.getenv("GRID_SPREAD", 0.005))
PRICE_RANGE_PCT = float(os.getenv("PRICE_RANGE_PCT", 0.05))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", 0.08))
MAX_OPEN_ORDERS = int(os.getenv("MAX_OPEN_ORDERS", 20))
MAKER_FEE       = float(os.getenv("MAKER_FEE", 0.0))    # 0% maker (MEXC)
TAKER_FEE       = float(os.getenv("TAKER_FEE", 0.001))  # 0.1% taker (MEXC)
MAX_FILLED_HISTORY = int(os.getenv("MAX_FILLED_HISTORY", 500))
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
PAPER_TRADING   = os.getenv("PAPER_TRADING", "true").lower() == "true"

# Chemin persistant dans le volume Docker (/app/data via STATE_FILE env)
DATA_DIR   = os.path.dirname(os.getenv("STATE_FILE", "/app/data/bot_state.json"))
STATE_FILE = os.getenv("STATE_FILE", "/app/data/bot_state.json")
LOG_FILE   = os.path.join(DATA_DIR, "bot.log")

os.makedirs(DATA_DIR, exist_ok=True)

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── STATE ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Impossible de charger l'état: {e} — état réinitialisé")
    return {
        "grid_orders": {},       # order_id -> order info
        "filled_orders": [],
        "total_profit": 0.0,
        "total_trades": 0,
        "start_time": datetime.now(TZ).isoformat(),
        "grid_base_price": None,
        "current_price": None,
        "balance": {},           # {asset: {free, used, total}} from exchange
        "portfolio_value": 0.0,
        "effective_capital": 0.0,
        "capital_allocation": CAPITAL_ALLOC,
        "start_portfolio_value": None,  # valeur initiale pour calculer le ROI
    }

def save_state(state: dict):
    # Tronquer l'historique pour éviter que le fichier grossisse indéfiniment
    if len(state.get("filled_orders", [])) > MAX_FILLED_HISTORY:
        state["filled_orders"] = state["filled_orders"][-MAX_FILLED_HISTORY:]
    # Écriture atomique : temp file + rename pour éviter corruption en cas de crash
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)

# ─── DISCORD ──────────────────────────────────────────────────────────────────

def notify(message: str, color: int = 0x00ff88):
    if not DISCORD_WEBHOOK:
        return
    try:
        payload = {
            "embeds": [{
                "description": message,
                "color": color,
                "footer": {"text": f"Grid Bot | {SYMBOL} | {'📄 PAPER' if PAPER_TRADING else '🔴 LIVE'}"},
                "timestamp": datetime.now(TZ).isoformat()
            }]
        }
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Discord notification failed: {e}")

# ─── EXCHANGE ─────────────────────────────────────────────────────────────────

def init_exchange() -> ccxt.Exchange:
    exchange_class = getattr(ccxt, EXCHANGE_ID)
    exchange = exchange_class({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    # Vérifier la connexion
    exchange.load_markets()
    log.info(f"✅ Connecté à {EXCHANGE_ID.upper()} — {SYMBOL} disponible: {SYMBOL in exchange.markets}")
    return exchange

# ─── BALANCE ─────────────────────────────────────────────────────────────────

def fetch_balance(exchange, state: dict, price: float = None):
    """Récupère le solde, calcule la valeur du portefeuille et le capital effectif."""
    try:
        base_asset = SYMBOL.split("/")[0]   # ex: BTC
        quote_asset = SYMBOL.split("/")[1]  # ex: USDT
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

        # Valeur totale du portefeuille en quote (USDT)
        p = price or state.get("current_price") or state.get("grid_base_price") or 0
        portfolio_value = q["total"] + b["total"] * p
        effective_capital = portfolio_value * CAPITAL_ALLOC / 100

        state["portfolio_value"] = round(portfolio_value, 4)
        state["effective_capital"] = round(effective_capital, 4)
        state["capital_allocation"] = CAPITAL_ALLOC

    except Exception as e:
        log.warning(f"Impossible de récupérer le solde: {e}")

# ─── PRIX & VOLATILITÉ ────────────────────────────────────────────────────────

def get_current_price(exchange) -> float:
    ticker = exchange.fetch_ticker(SYMBOL)
    return float(ticker["last"])

def get_volatility(exchange) -> float:
    """Volatilité hourly sur 24h (écart-type des rendements en %)."""
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, "1h", limit=25)
        closes = [c[4] for c in ohlcv]
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance) * 100
    except Exception as e:
        log.warning(f"Impossible de calculer la volatilité: {e}")
        return 0.0

# ─── GRILLE ───────────────────────────────────────────────────────────────────

def compute_grid(base_price: float) -> list:
    """Génère les niveaux buy/sell autour du prix de base."""
    levels = []
    for i in range(1, GRID_LEVELS + 1):
        levels.append({
            "price": round(base_price * (1 - i * GRID_SPREAD), 2),
            "side": "buy",
            "index": i
        })
        levels.append({
            "price": round(base_price * (1 + i * GRID_SPREAD), 2),
            "side": "sell",
            "index": i
        })
    return levels

def order_size(base_price: float, capital: float) -> float:
    """Taille d'ordre en BTC par niveau (moitié du capital par côté)."""
    size_usdt = (capital / 2) / GRID_LEVELS
    return round(size_usdt / base_price, 6)

# ─── PLACEMENT DE LA GRILLE ───────────────────────────────────────────────────

def place_grid(exchange, state: dict):
    """Annule les ordres existants et place une nouvelle grille."""
    # Annuler tous les ordres ouverts
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)
        for o in open_orders:
            try:
                exchange.cancel_order(o["id"], SYMBOL)
            except Exception as e:
                log.warning(f"Erreur annulation ordre {o['id']}: {e}")
        if open_orders:
            log.info(f"🗑️ {len(open_orders)} ordres existants annulés")
    except Exception as e:
        log.warning(f"Erreur lors de la récupération des ordres ouverts: {e}")

    price = get_current_price(exchange)
    state["grid_base_price"] = price

    # Calculer le capital effectif depuis le solde exchange
    fetch_balance(exchange, state, price)
    capital = state.get("effective_capital", 0)
    if capital < MIN_CAPITAL:
        msg = f"❌ Capital insuffisant: {capital:.2f} USDT (minimum: {MIN_CAPITAL:.0f} USDT) — bot arrêté"
        log.error(msg)
        notify(msg, color=0xff0000)
        raise SystemExit(msg)

    size = order_size(price, capital)
    grid = compute_grid(price)

    log.info(f"📐 Grille @ {price:.2f} | Capital: {capital:.2f} USDT ({CAPITAL_ALLOC:.0f}% de {state.get('portfolio_value', 0):.2f}) | taille: {size} BTC")

    placed = {}
    errors = 0
    for level in grid:
        if len(placed) >= MAX_OPEN_ORDERS:
            log.warning(f"⚠️ Limite MAX_OPEN_ORDERS ({MAX_OPEN_ORDERS}) atteinte")
            break
        try:
            if level["side"] == "buy":
                order = exchange.create_limit_buy_order(SYMBOL, size, level["price"])
            else:
                order = exchange.create_limit_sell_order(SYMBOL, size, level["price"])

            oid = str(order.get("id", f"paper_{level['side']}_{level['index']}"))
            placed[oid] = {
                "id": oid,
                "side": level["side"],
                "price": level["price"],
                "size": size,
                "index": level["index"],
                "placed_at": datetime.now(TZ).isoformat(),
                "is_counter": False,
            }
            log.info(f"  ✅ {level['side'].upper()} @ {level['price']:.2f}")
        except Exception as e:
            log.warning(f"  ❌ Erreur {level['side']} @ {level['price']:.2f}: {e}")
            errors += 1

    state["grid_orders"] = placed
    save_state(state)

    notify(
        f"🚀 **Grille placée — {SYMBOL}**\n"
        f"Prix: `{price:.2f}` | Niveaux: `{GRID_LEVELS}×2` | Spread: `{GRID_SPREAD*100:.1f}%`\n"
        f"Taille/niveau: `{size} BTC` | Capital: `{capital:.2f} USDT` ({CAPITAL_ALLOC:.0f}%)\n"
        f"Ordres placés: `{len(placed)}` | Erreurs: `{errors}`"
    )

# ─── REBALANCE (PRÉSERVE LES CONTRE-ORDRES) ──────────────────────────────────

def rebalance_grid(exchange, state: dict, new_price: float):
    """Recentre la grille en préservant les contre-ordres actifs."""
    grid_orders = state.get("grid_orders", {})

    # Séparer les contre-ordres (cycles en cours) des ordres initiaux
    counter_orders = {oid: o for oid, o in grid_orders.items() if o.get("is_counter")}
    initial_orders = {oid: o for oid, o in grid_orders.items() if not o.get("is_counter")}

    # Annuler uniquement les ordres initiaux sur l'exchange
    for oid in initial_orders:
        try:
            exchange.cancel_order(oid, SYMBOL)
        except Exception:
            pass

    log.info(f"♻️ Rebalance: {len(initial_orders)} ordres initiaux annulés, {len(counter_orders)} contre-ordres préservés")

    # Recalculer le capital effectif (compounding)
    state["grid_base_price"] = new_price
    fetch_balance(exchange, state, new_price)
    capital = state.get("effective_capital", 0)
    if capital < MIN_CAPITAL:
        msg = f"❌ Capital insuffisant pour rebalance: {capital:.2f} USDT (minimum: {MIN_CAPITAL:.0f} USDT) — bot arrêté"
        log.error(msg)
        notify(msg, color=0xff0000)
        raise SystemExit(msg)

    size = order_size(new_price, capital)
    grid = compute_grid(new_price)

    new_orders = dict(counter_orders)  # Garder les contre-ordres
    errors = 0
    for level in grid:
        if len(new_orders) >= MAX_OPEN_ORDERS:
            break
        try:
            if level["side"] == "buy":
                order = exchange.create_limit_buy_order(SYMBOL, size, level["price"])
            else:
                order = exchange.create_limit_sell_order(SYMBOL, size, level["price"])

            oid = str(order.get("id", f"paper_{level['side']}_{level['index']}"))
            new_orders[oid] = {
                "id": oid,
                "side": level["side"],
                "price": level["price"],
                "size": size,
                "index": level["index"],
                "placed_at": datetime.now(TZ).isoformat(),
                "is_counter": False,
            }
        except Exception as e:
            log.warning(f"  ❌ Erreur {level['side']} @ {level['price']:.2f}: {e}")
            errors += 1

    state["grid_orders"] = new_orders
    save_state(state)
    log.info(f"📐 Rebalance terminée @ {new_price:.2f} | {len(new_orders)} ordres actifs (dont {len(counter_orders)} contre-ordres)")

# ─── DÉTECTION DES FILLS (LIVE) ───────────────────────────────────────────────

def check_fills_live(exchange, state: dict):
    """
    Compare les ordres ouverts sur l'exchange avec notre état.
    Si un ordre a disparu → il a été rempli → on enregistre et on replace un contre-ordre.
    """
    try:
        open_order_ids = {str(o["id"]) for o in exchange.fetch_open_orders(SYMBOL)}
    except Exception as e:
        log.warning(f"Erreur fetch_open_orders: {e}")
        return

    grid_orders = state.get("grid_orders", {})
    missing_ids = [oid for oid in grid_orders if oid not in open_order_ids]

    for oid in missing_ids:
        # Vérifier si l'ordre a vraiment été rempli (pas annulé)
        try:
            order_info = exchange.fetch_order(oid, SYMBOL)
            status = order_info.get("status", "")
            if status in ("canceled", "cancelled", "expired", "rejected"):
                log.warning(f"⚠️ Ordre {oid} annulé/expiré sur l'exchange (status: {status}) — ignoré")
                grid_orders.pop(oid)
                continue
        except Exception:
            pass  # Si fetch_order échoue, on suppose fill (comportement legacy)

        order = grid_orders.pop(oid)
        fill_price = order["price"]
        size = order["size"]
        side = order["side"]
        is_counter = order.get("is_counter", False)

        # Frais maker sur chaque fill (ordres limit = toujours maker)
        fee = fill_price * size * MAKER_FEE
        # Profit uniquement sur les contre-ordres (cycle buy+sell complété)
        profit = -fee
        if is_counter:
            # Profit du cycle = spread - frais des 2 fills (ce fill + le fill initial)
            gross = fill_price * GRID_SPREAD * size
            profit = gross - 2 * fee
        state["total_profit"] += profit
        state["total_trades"] += 1

        fill_record = {**order, "fill_time": datetime.now(TZ).isoformat(), "profit": profit}
        state["filled_orders"].append(fill_record)

        if is_counter:
            log.info(f"💰 FILL {side.upper()} @ {fill_price:.2f} | +{profit:.4f} USDT net (cycle complété, frais: {2*fee:.4f})")
        else:
            log.info(f"📥 FILL {side.upper()} @ {fill_price:.2f} | ordre initial (frais: {fee:.4f})")

        # Placer le contre-ordre
        try:
            if side == "buy":
                counter_price = round(fill_price * (1 + GRID_SPREAD), 2)
                new_order = exchange.create_limit_sell_order(SYMBOL, size, counter_price)
            else:
                counter_price = round(fill_price * (1 - GRID_SPREAD), 2)
                new_order = exchange.create_limit_buy_order(SYMBOL, size, counter_price)

            new_oid = str(new_order.get("id", f"counter_{oid}"))
            state["grid_orders"][new_oid] = {
                "id": new_oid,
                "side": "sell" if side == "buy" else "buy",
                "price": counter_price,
                "size": size,
                "index": order.get("index", 0),
                "placed_at": datetime.now(TZ).isoformat(),
                "is_counter": True,
            }
            log.info(f"  ↩️ Contre-ordre {'SELL' if side == 'buy' else 'BUY'} @ {counter_price:.2f}")
        except Exception as e:
            log.warning(f"  ❌ Erreur contre-ordre: {e}")

    state["grid_orders"] = grid_orders

# ─── PAPER TRADING ────────────────────────────────────────────────────────────

PAPER_BALANCE = float(os.getenv("PAPER_BALANCE", 100))  # Solde USDT simulé

class PaperExchange:
    """Simulateur d'exchange pour paper trading — utilise les vrais prix du marché."""

    def __init__(self):
        self._real_exchange = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})
        self._real_exchange.load_markets()
        self.orders: dict = {}
        self._order_id = 1000
        self._usdt = PAPER_BALANCE
        self._btc = 0.0
        self._last_price = 0.0

    def _new_id(self) -> str:
        self._order_id += 1
        return f"paper_{self._order_id}"

    def load_markets(self):
        return self._real_exchange.markets

    def fetch_ticker(self, symbol: str) -> dict:
        ticker = self._real_exchange.fetch_ticker(symbol)
        self._last_price = float(ticker["last"])
        return ticker

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 24) -> list:
        return self._real_exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    def _create_order(self, side: str, symbol: str, amount: float, price: float) -> dict:
        oid = self._new_id()
        self.orders[oid] = {
            "id": oid, "side": side, "amount": amount,
            "price": price, "status": "open", "symbol": symbol
        }
        return self.orders[oid]

    def create_limit_buy_order(self, symbol: str, amount: float, price: float) -> dict:
        return self._create_order("buy", symbol, amount, price)

    def create_limit_sell_order(self, symbol: str, amount: float, price: float) -> dict:
        return self._create_order("sell", symbol, amount, price)

    def fetch_open_orders(self, symbol: str) -> list:
        self._simulate_fills()
        return list(self.orders.values())

    def cancel_order(self, oid: str, symbol: str):
        if oid in self.orders:
            del self.orders[oid]

    def fetch_order(self, oid: str, symbol: str) -> dict:
        return {"id": oid, "status": "closed"}

    def fetch_balance(self):
        base_asset = SYMBOL.split("/")[0]
        quote_asset = SYMBOL.split("/")[1]
        used_usdt = sum(o["amount"] * o["price"] for o in self.orders.values() if o["side"] == "buy")
        used_btc = sum(o["amount"] for o in self.orders.values() if o["side"] == "sell")
        return {
            quote_asset: {"free": self._usdt - used_usdt, "used": used_usdt, "total": self._usdt},
            base_asset: {"free": self._btc - used_btc, "used": used_btc, "total": self._btc},
        }

    def _simulate_fills(self):
        """Simule les fills quand le vrai prix croise un niveau."""
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


# ─── BOUCLE PRINCIPALE ────────────────────────────────────────────────────────

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
    roi = (state["total_profit"] / start_val * 100)
    capital = state.get("effective_capital", 0)
    portfolio = state.get("portfolio_value", 0)
    return (
        f"📊 **Rapport Grid Bot**\n"
        f"Durée: `{elapsed}` | Trades: `{state['total_trades']}`\n"
        f"Profit: `{state['total_profit']:.4f} USDT` | ROI: `{roi:.2f}%`\n"
        f"Portefeuille: `{portfolio:.2f} USDT` | Capital alloué: `{capital:.2f} USDT` ({CAPITAL_ALLOC:.0f}%)\n"
        f"Ordres actifs: `{len(state.get('grid_orders', {}))}`"
    )


def main():
    log.info("=" * 60)
    log.info("🤖 Grid Trading Bot — démarrage")
    log.info(f"   Exchange : {EXCHANGE_ID.upper()}")
    log.info(f"   Paire    : {SYMBOL}")
    log.info(f"   Allocation: {CAPITAL_ALLOC}% du portefeuille")
    log.info(f"   Mode     : {'📄 PAPER TRADING' if PAPER_TRADING else '🔴 LIVE'}")
    log.info("=" * 60)

    state = load_state()

    if PAPER_TRADING:
        exchange = PaperExchange()
        log.info("📄 Paper exchange initialisé (prix réels via API publique)")
    else:
        try:
            exchange = init_exchange()
        except Exception as e:
            log.error(f"❌ Impossible de se connecter à {EXCHANGE_ID}: {e}")
            notify(f"❌ **Erreur de connexion**\n`{e}`", color=0xff0000)
            raise

    # Snapshot initial du portefeuille
    init_price = get_current_price(exchange)
    fetch_balance(exchange, state, init_price)
    state["current_price"] = init_price
    if not state.get("start_portfolio_value"):
        state["start_portfolio_value"] = state.get("portfolio_value", 0)
    log.info(f"💰 Portefeuille: {state['portfolio_value']:.2f} USDT | Capital alloué: {state['effective_capital']:.2f} USDT ({CAPITAL_ALLOC:.0f}%)")

    if state["effective_capital"] < MIN_CAPITAL:
        msg = f"❌ Capital insuffisant: {state['effective_capital']:.2f} USDT < {MIN_CAPITAL:.0f} USDT minimum — bot arrêté"
        log.error(msg)
        notify(msg, color=0xff0000)
        raise SystemExit(msg)

    # Volatilité initiale
    vol = get_volatility(exchange)
    log.info(f"📈 Volatilité 1h/24h: {vol:.3f}% — {'🔥 Favorable' if vol > 0.5 else '😴 Faible'}")

    # Reprise ou placement initial de la grille
    if state.get("grid_orders") and state.get("grid_base_price"):
        log.info(f"♻️ Reprise de la grille existante ({len(state['grid_orders'])} ordres, base: {state['grid_base_price']:.2f})")
        # Vérifier que les ordres existent encore sur l'exchange
        try:
            open_ids = {str(o["id"]) for o in exchange.fetch_open_orders(SYMBOL)}
            orphans = [oid for oid in state["grid_orders"] if oid not in open_ids]
            if orphans:
                log.warning(f"⚠️ {len(orphans)} ordres orphelins détectés (plus sur l'exchange)")
                for oid in orphans:
                    del state["grid_orders"][oid]
            if not state["grid_orders"]:
                log.info("🔄 Aucun ordre valide restant — replaçement de la grille")
                place_grid(exchange, state)
        except Exception as e:
            log.warning(f"Impossible de vérifier les ordres existants: {e} — replaçement")
            place_grid(exchange, state)
    else:
        place_grid(exchange, state)
    notify("✅ **Grid Bot démarré**\nSurveillance toutes les 30s.")

    last_report_hour = None  # Pour éviter d'envoyer le rapport 2x dans la même heure

    while True:
        try:
            time.sleep(30)
            price = get_current_price(exchange)
            state["current_price"] = price

            # Détecter les fills et placer les contre-ordres
            check_fills_live(exchange, state)

            # Stop loss global (basé sur la valeur initiale du portefeuille)
            start_val = state.get("start_portfolio_value") or state.get("effective_capital") or 1
            if state["total_profit"] < -(start_val * STOP_LOSS_PCT):
                msg = f"🛑 **STOP LOSS DÉCLENCHÉ**\nPerte: `{state['total_profit']:.4f} USDT` ({STOP_LOSS_PCT*100:.0f}%)\nBot arrêté."
                log.error(msg)
                notify(msg, color=0xff0000)
                save_state(state)
                raise SystemExit(msg)

            # Recentrer la grille si le prix dérive trop
            base = state.get("grid_base_price") or price
            drift = abs(price - base) / base
            if drift > PRICE_RANGE_PCT:
                log.info(f"🔄 Drift {drift*100:.1f}% > {PRICE_RANGE_PCT*100:.0f}% — recentrage de la grille")
                rebalance_grid(exchange, state, price)

            # Mise à jour du solde
            fetch_balance(exchange, state)

            # Rapport Discord à 8h et 20h (heure de Paris)
            now = datetime.now(TZ)
            if now.hour in (8, 20) and last_report_hour != now.hour:
                last_report_hour = now.hour
                report = status_report(state)
                log.info(report.replace("**", "").replace("`", ""))
                notify(report)
            elif now.hour not in (8, 20):
                last_report_hour = None  # Reset pour le prochain créneau

            save_state(state)

        except KeyboardInterrupt:
            log.info("⏹️ Arrêt manuel")
            notify(status_report(state) + "\n\n⏹️ Bot arrêté manuellement.")
            save_state(state)
            break
        except Exception as e:
            log.error(f"Erreur inattendue: {e}", exc_info=True)
            notify(f"⚠️ **Erreur**\n`{e}`\nReprise dans 60s.", color=0xff8800)
            time.sleep(60)


if __name__ == "__main__":
    main()
