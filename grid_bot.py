#!/usr/bin/env python3
"""
Grid Trading Bot — Stratégie volatilité
Auteur : Claude / OpenClaw
Description : Bot de grid trading qui profite de la volatilité en achetant bas et vendant haut
              dans une grille de prix auto-ajustable.
"""

import ccxt
import os
import time
import json
import logging
import requests
from datetime import datetime
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

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── STATE ────────────────────────────────────────────────────────────────────

STATE_FILE = "bot_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "grid_orders": [],
        "filled_orders": [],
        "total_profit": 0.0,
        "total_trades": 0,
        "start_capital": CAPITAL,
        "start_time": datetime.now().isoformat(),
        "grid_base_price": None,
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─── DISCORD NOTIFICATIONS ────────────────────────────────────────────────────

def notify(message: str, color: int = 0x00ff88):
    """Envoie une notification Discord."""
    if not DISCORD_WEBHOOK:
        return
    try:
        payload = {
            "embeds": [{
                "description": message,
                "color": color,
                "footer": {"text": f"Grid Bot | {SYMBOL} | {'📄 PAPER' if PAPER_TRADING else '🔴 LIVE'}"},
                "timestamp": datetime.utcnow().isoformat()
            }]
        }
        requests.post(DISCORD_WEBHOOK, json=payload, timeout=5)
    except Exception as e:
        log.warning(f"Discord notification failed: {e}")

# ─── EXCHANGE ─────────────────────────────────────────────────────────────────

def init_exchange():
    exchange_class = getattr(ccxt, EXCHANGE_ID)
    exchange = exchange_class({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if PAPER_TRADING:
        log.info("🟡 MODE PAPER TRADING ACTIVÉ — aucun ordre réel ne sera passé")
    else:
        log.info(f"🔴 MODE LIVE sur {EXCHANGE_ID.upper()}")
    return exchange

# ─── PRIX ─────────────────────────────────────────────────────────────────────

def get_current_price(exchange) -> float:
    ticker = exchange.fetch_ticker(SYMBOL)
    return float(ticker["last"])

def get_volatility(exchange, timeframe="1h", periods=24) -> float:
    """Calcule la volatilité sur les N dernières bougies (annualisée en %)."""
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe, limit=periods)
    closes = [c[4] for c in ohlcv]
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    import math
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance) * 100  # en %

# ─── GRILLE ───────────────────────────────────────────────────────────────────

def compute_grid(base_price: float) -> list[dict]:
    """
    Génère les niveaux de la grille autour du prix actuel.
    Retourne une liste de niveaux avec prix et type (buy/sell).
    """
    levels = []
    spread = GRID_SPREAD

    for i in range(1, GRID_LEVELS + 1):
        buy_price  = round(base_price * (1 - i * spread), 2)
        sell_price = round(base_price * (1 + i * spread), 2)
        levels.append({"price": buy_price,  "side": "buy",  "index": i})
        levels.append({"price": sell_price, "side": "sell", "index": i})

    return levels

def order_size_per_level(base_price: float) -> float:
    """Calcule la taille d'ordre par niveau en USDT."""
    capital_per_side = CAPITAL / 2  # moitié buy, moitié sell
    size_usdt = capital_per_side / GRID_LEVELS
    size_base = size_usdt / base_price
    return round(size_base, 6)

# ─── PAPER TRADING ────────────────────────────────────────────────────────────

class PaperExchange:
    """Simulateur d'exchange pour paper trading."""

    def __init__(self, base_price):
        self.price = base_price
        self.orders = {}
        self.order_id = 1

    def fetch_ticker(self, symbol):
        # Simule une petite variation de prix
        import random
        self.price *= (1 + random.uniform(-0.002, 0.002))
        return {"last": self.price, "symbol": symbol}

    def fetch_ohlcv(self, symbol, timeframe, limit=24):
        # Données fictives pour paper trading
        import random
        price = self.price
        data = []
        for i in range(limit):
            price *= (1 + random.uniform(-0.01, 0.01))
            data.append([0, price*0.99, price*1.01, price*0.98, price, 100])
        return data

    def create_limit_buy_order(self, symbol, amount, price):
        oid = str(self.order_id)
        self.order_id += 1
        self.orders[oid] = {"id": oid, "side": "buy", "amount": amount, "price": price, "status": "open"}
        return self.orders[oid]

    def create_limit_sell_order(self, symbol, amount, price):
        oid = str(self.order_id)
        self.order_id += 1
        self.orders[oid] = {"id": oid, "side": "sell", "amount": amount, "price": price, "status": "open"}
        return self.orders[oid]

    def fetch_open_orders(self, symbol):
        return [o for o in self.orders.values() if o["status"] == "open"]

    def cancel_order(self, oid, symbol):
        if oid in self.orders:
            self.orders[oid]["status"] = "canceled"

    def check_fills(self, current_price):
        """Simule les fills selon le prix actuel."""
        filled = []
        for oid, order in self.orders.items():
            if order["status"] != "open":
                continue
            if order["side"] == "buy" and current_price <= order["price"]:
                order["status"] = "filled"
                filled.append(order)
            elif order["side"] == "sell" and current_price >= order["price"]:
                order["status"] = "filled"
                filled.append(order)
        return filled

# ─── LOGIQUE PRINCIPALE ───────────────────────────────────────────────────────

def place_grid_orders(exchange, state: dict):
    """Place tous les ordres de la grille."""
    price = get_current_price(exchange)
    state["grid_base_price"] = price
    size = order_size_per_level(price)
    grid = compute_grid(price)

    log.info(f"📐 Grille générée autour de {price:.2f} — {len(grid)} niveaux, taille: {size} BTC/niveau")

    placed = []
    for level in grid:
        try:
            if level["side"] == "buy":
                order = exchange.create_limit_buy_order(SYMBOL, size, level["price"])
            else:
                order = exchange.create_limit_sell_order(SYMBOL, size, level["price"])
            placed.append({**level, "order_id": order.get("id", "paper"), "size": size})
            log.info(f"  ✅ {level['side'].upper()} @ {level['price']:.2f} — {size} BTC")
        except Exception as e:
            log.warning(f"  ❌ Erreur ordre {level['side']} @ {level['price']}: {e}")

    state["grid_orders"] = placed
    save_state(state)

    notify(
        f"🚀 **Grille placée sur {SYMBOL}**\n"
        f"Prix de base: `{price:.2f}`\n"
        f"Niveaux: `{GRID_LEVELS}` de chaque côté\n"
        f"Spread: `{GRID_SPREAD*100:.1f}%`\n"
        f"Taille/niveau: `{size} BTC`\n"
        f"Capital alloué: `{CAPITAL} USDT`"
    )

def check_and_rebalance(exchange, state: dict):
    """Vérifie les ordres, détecte les fills, replace les ordres manquants."""
    price = get_current_price(exchange)

    # Paper trading: simuler les fills
    if PAPER_TRADING and isinstance(exchange, PaperExchange):
        filled = exchange.check_fills(price)
        for order in filled:
            profit = 0.0
            if order["side"] == "sell":
                profit = (order["price"] - state["grid_base_price"]) * order["amount"] * GRID_SPREAD
            state["total_profit"] += profit
            state["total_trades"] += 1
            state["filled_orders"].append({**order, "fill_time": datetime.now().isoformat(), "profit": profit})
            log.info(f"💰 FILL {order['side'].upper()} @ {order['price']:.2f} | Profit estimé: {profit:.4f} USDT")

    # Stop loss global
    if state["total_profit"] < -(CAPITAL * STOP_LOSS_PCT):
        log.error(f"🛑 STOP LOSS GLOBAL DÉCLENCHÉ — perte: {state['total_profit']:.2f} USDT")
        notify(f"🛑 **STOP LOSS DÉCLENCHÉ**\nPerte totale: `{state['total_profit']:.2f} USDT`\nBot arrêté.", color=0xff0000)
        raise SystemExit("Stop loss triggered")

    # Vérifier si la grille doit être recalculée (prix sorti de la zone)
    base = state.get("grid_base_price", price)
    drift = abs(price - base) / base
    if drift > PRICE_RANGE_PCT:
        log.info(f"📊 Prix sorti de la grille ({drift*100:.1f}% de drift) — recentrage...")
        try:
            for order in exchange.fetch_open_orders(SYMBOL):
                exchange.cancel_order(order["id"], SYMBOL)
        except Exception as e:
            log.warning(f"Erreur annulation: {e}")
        place_grid_orders(exchange, state)

    save_state(state)

def print_status(state: dict):
    """Affiche un résumé du bot."""
    elapsed = ""
    try:
        start = datetime.fromisoformat(state["start_time"])
        delta = datetime.now() - start
        hours = int(delta.total_seconds() // 3600)
        elapsed = f"{hours}h"
    except:
        pass

    log.info(
        f"\n{'='*50}\n"
        f"  📊 STATUS GRID BOT — {SYMBOL}\n"
        f"  Mode: {'📄 PAPER' if PAPER_TRADING else '🔴 LIVE'}\n"
        f"  Durée: {elapsed}\n"
        f"  Trades: {state['total_trades']}\n"
        f"  Profit total: {state['total_profit']:.4f} USDT\n"
        f"  ROI: {(state['total_profit'] / CAPITAL * 100):.2f}%\n"
        f"{'='*50}"
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    log.info("🤖 Grid Trading Bot démarré")
    log.info(f"  Exchange: {EXCHANGE_ID} | Paire: {SYMBOL} | Capital: {CAPITAL} USDT")

    state = load_state()

    if PAPER_TRADING:
        price = 65000.0  # Prix simulé de départ
        exchange = PaperExchange(price)
        log.info(f"📄 Paper trading initialisé — prix simulé: {price}")
    else:
        exchange = init_exchange()

    # Calculer et afficher la volatilité
    try:
        vol = get_volatility(exchange)
        log.info(f"📈 Volatilité 24h: {vol:.2f}% — {'🔥 Favorable' if vol > 1 else '😴 Faible'}")
    except:
        pass

    # Placer la grille initiale
    place_grid_orders(exchange, state)
    notify("✅ **Grid Bot démarré**\nSurveillance active toutes les 30 secondes.")

    # Boucle principale
    loop_count = 0
    while True:
        try:
            time.sleep(30)
            check_and_rebalance(exchange, state)
            loop_count += 1

            # Status toutes les 10 min
            if loop_count % 20 == 0:
                print_status(state)
                notify(
                    f"📊 **Rapport Grid Bot**\n"
                    f"Trades: `{state['total_trades']}`\n"
                    f"Profit: `{state['total_profit']:.4f} USDT`\n"
                    f"ROI: `{(state['total_profit'] / CAPITAL * 100):.2f}%`"
                )

        except KeyboardInterrupt:
            log.info("⏹️ Bot arrêté manuellement")
            print_status(state)
            break
        except SystemExit:
            break
        except Exception as e:
            log.error(f"Erreur inattendue: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    main()
