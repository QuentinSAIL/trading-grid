#!/usr/bin/env python3
"""
Grid Trading Bot — Stratégie volatilité
Exchange : MEXC (maker 0%, taker 0.1%)
"""

import ccxt
import os
import math
import random
import time
import json
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

EXCHANGE_ID     = os.getenv("EXCHANGE", "mexc")
API_KEY         = os.getenv("API_KEY", "")
API_SECRET      = os.getenv("API_SECRET", "")
SYMBOL          = os.getenv("SYMBOL", "BTC/USDT")
CAPITAL         = float(os.getenv("CAPITAL", 80))
GRID_LEVELS     = int(os.getenv("GRID_LEVELS", 10))
GRID_SPREAD     = float(os.getenv("GRID_SPREAD", 0.005))
PRICE_RANGE_PCT = float(os.getenv("PRICE_RANGE_PCT", 0.05))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", 0.08))
MAX_OPEN_ORDERS = int(os.getenv("MAX_OPEN_ORDERS", 20))
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
        logging.FileHandler(LOG_FILE),
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
        "start_capital": CAPITAL,
        "start_time": datetime.now().isoformat(),
        "grid_base_price": None,
        "current_price": None,
        "balance": {},           # {asset: {free, used, total}} from exchange
    }

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

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
                "timestamp": datetime.now(timezone.utc).isoformat()
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

def fetch_balance(exchange, state: dict):
    """Récupère le solde USDT et l'asset tradé depuis l'exchange."""
    try:
        base_asset = SYMBOL.split("/")[0]   # ex: BTC
        quote_asset = SYMBOL.split("/")[1]  # ex: USDT
        balance = exchange.fetch_balance()
        state["balance"] = {
            quote_asset: {
                "free": float(balance.get(quote_asset, {}).get("free", 0) or 0),
                "used": float(balance.get(quote_asset, {}).get("used", 0) or 0),
                "total": float(balance.get(quote_asset, {}).get("total", 0) or 0),
            },
            base_asset: {
                "free": float(balance.get(base_asset, {}).get("free", 0) or 0),
                "used": float(balance.get(base_asset, {}).get("used", 0) or 0),
                "total": float(balance.get(base_asset, {}).get("total", 0) or 0),
            },
        }
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

def order_size(base_price: float) -> float:
    """Taille d'ordre en BTC par niveau (moitié du capital par côté)."""
    size_usdt = (CAPITAL / 2) / GRID_LEVELS
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
    size = order_size(price)
    grid = compute_grid(price)

    log.info(f"📐 Grille @ {price:.2f} | {GRID_LEVELS} niveaux × {GRID_SPREAD*100:.2f}% | taille: {size} BTC")

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
                "placed_at": datetime.now().isoformat(),
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
        f"Taille/niveau: `{size} BTC` | Capital: `{CAPITAL} USDT`\n"
        f"Ordres placés: `{len(placed)}` | Erreurs: `{errors}`"
    )

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
    filled_ids = [oid for oid in grid_orders if oid not in open_order_ids]

    for oid in filled_ids:
        order = grid_orders.pop(oid)
        fill_price = order["price"]
        size = order["size"]
        side = order["side"]
        is_counter = order.get("is_counter", False)

        # Profit uniquement sur les contre-ordres (cycle buy+sell complété)
        profit = 0.0
        if is_counter:
            profit = fill_price * GRID_SPREAD * size
            state["total_profit"] += profit
        state["total_trades"] += 1

        fill_record = {**order, "fill_time": datetime.now().isoformat(), "profit": profit}
        state["filled_orders"].append(fill_record)

        if is_counter:
            log.info(f"💰 FILL {side.upper()} @ {fill_price:.2f} | +{profit:.4f} USDT (cycle complété)")
        else:
            log.info(f"📥 FILL {side.upper()} @ {fill_price:.2f} | ordre initial (pas encore de profit)")

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
                "placed_at": datetime.now().isoformat(),
                "is_counter": True,
            }
            log.info(f"  ↩️ Contre-ordre {'SELL' if side == 'buy' else 'BUY'} @ {counter_price:.2f}")
        except Exception as e:
            log.warning(f"  ❌ Erreur contre-ordre: {e}")

    state["grid_orders"] = grid_orders

# ─── PAPER TRADING ────────────────────────────────────────────────────────────

class PaperExchange:
    """Simulateur d'exchange pour paper trading."""

    def __init__(self, base_price: float):
        self.price = base_price
        self.orders: dict = {}
        self._order_id = 1000

    def _new_id(self) -> str:
        self._order_id += 1
        return f"paper_{self._order_id}"

    def load_markets(self):
        return {}

    def fetch_ticker(self, symbol: str) -> dict:
        self.price *= (1 + random.uniform(-0.003, 0.003))
        return {"last": self.price, "symbol": symbol}

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 24) -> list:
        price = self.price
        data = []
        for _ in range(limit):
            price *= (1 + random.uniform(-0.008, 0.008))
            data.append([0, price * 0.99, price * 1.01, price * 0.98, price, 100.0])
        return data

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
        # Simuler les fills avant de retourner les ordres ouverts
        self._simulate_fills()
        return [o for o in self.orders.values() if o["status"] == "open"]

    def cancel_order(self, oid: str, symbol: str):
        if oid in self.orders:
            self.orders[oid]["status"] = "canceled"

    def fetch_balance(self):
        base_asset = SYMBOL.split("/")[0]
        quote_asset = SYMBOL.split("/")[1]
        return {
            quote_asset: {"free": CAPITAL * 0.5, "used": CAPITAL * 0.5, "total": CAPITAL},
            base_asset: {"free": 0.001, "used": 0.001, "total": 0.002},
        }

    def _simulate_fills(self):
        """Simule des fills aléatoires sur les ordres ouverts."""
        for order in self.orders.values():
            if order["status"] != "open":
                continue
            if order["side"] == "buy" and self.price <= order["price"]:
                order["status"] = "filled"
            elif order["side"] == "sell" and self.price >= order["price"]:
                order["status"] = "filled"


# ─── BOUCLE PRINCIPALE ────────────────────────────────────────────────────────

def status_report(state: dict) -> str:
    elapsed = "N/A"
    try:
        start = datetime.fromisoformat(state["start_time"])
        delta = datetime.now() - start
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        elapsed = f"{h}h{m:02d}m"
    except Exception:
        pass

    roi = (state["total_profit"] / CAPITAL * 100) if CAPITAL else 0
    return (
        f"📊 **Rapport Grid Bot**\n"
        f"Durée: `{elapsed}` | Trades: `{state['total_trades']}`\n"
        f"Profit: `{state['total_profit']:.4f} USDT` | ROI: `{roi:.2f}%`\n"
        f"Ordres actifs: `{len(state.get('grid_orders', {}))}`"
    )


def main():
    log.info("=" * 60)
    log.info("🤖 Grid Trading Bot — démarrage")
    log.info(f"   Exchange : {EXCHANGE_ID.upper()}")
    log.info(f"   Paire    : {SYMBOL}")
    log.info(f"   Capital  : {CAPITAL} USDT")
    log.info(f"   Mode     : {'📄 PAPER TRADING' if PAPER_TRADING else '🔴 LIVE'}")
    log.info("=" * 60)

    state = load_state()

    if PAPER_TRADING:
        exchange = PaperExchange(base_price=87000.0)
        log.info("📄 Paper exchange initialisé (prix simulé: 87000)")
    else:
        try:
            exchange = init_exchange()
        except Exception as e:
            log.error(f"❌ Impossible de se connecter à {EXCHANGE_ID}: {e}")
            notify(f"❌ **Erreur de connexion**\n`{e}`", color=0xff0000)
            raise

    # Volatilité initiale
    vol = get_volatility(exchange)
    log.info(f"📈 Volatilité 1h/24h: {vol:.3f}% — {'🔥 Favorable' if vol > 0.5 else '😴 Faible'}")

    # Placer la grille initiale
    place_grid(exchange, state)
    notify("✅ **Grid Bot démarré**\nSurveillance toutes les 30s.")

    loop_count = 0
    rebalance_interval = 20  # status toutes les ~10 min (20 × 30s)

    while True:
        try:
            time.sleep(30)
            loop_count += 1
            price = get_current_price(exchange)
            state["current_price"] = price

            # Détecter les fills et placer les contre-ordres
            check_fills_live(exchange, state)

            # Stop loss global
            if state["total_profit"] < -(CAPITAL * STOP_LOSS_PCT):
                msg = f"🛑 **STOP LOSS DÉCLENCHÉ**\nPerte: `{state['total_profit']:.4f} USDT` ({STOP_LOSS_PCT*100:.0f}%)\nBot arrêté."
                log.error(msg)
                notify(msg, color=0xff0000)
                save_state(state)
                break

            # Recentrer la grille si le prix dérive trop
            base = state.get("grid_base_price") or price
            drift = abs(price - base) / base
            if drift > PRICE_RANGE_PCT:
                log.info(f"🔄 Drift {drift*100:.1f}% > {PRICE_RANGE_PCT*100:.0f}% — recentrage de la grille")
                place_grid(exchange, state)

            # Mise à jour du solde
            fetch_balance(exchange, state)

            # Rapport périodique
            if loop_count % rebalance_interval == 0:
                report = status_report(state)
                log.info(report.replace("**", "").replace("`", ""))
                notify(report)

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
