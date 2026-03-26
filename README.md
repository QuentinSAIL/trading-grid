# 🤖 Grid Trading Bot

Stratégie : **Grid Trading** — profite de la volatilité en achetant bas / vendant haut automatiquement.

## Comment ça marche

Le bot place une grille d'ordres buy et sell autour du prix actuel :
```
SELL @ 65975  ← niveau +3
SELL @ 65650  ← niveau +2  
SELL @ 65325  ← niveau +1
─── PRIX ACTUEL : 65000 ───
BUY  @ 64675  ← niveau -1
BUY  @ 64350  ← niveau -2
BUY  @ 64025  ← niveau -3
```
À chaque oscillation du marché, des ordres se remplissent → profit.

## Installation (Docker — recommandé)

```bash
git clone git@github.com:QuentinSAIL/trading-grid.git
cd trading-grid
cp .env.example .env
# Éditer .env avec tes infos
mkdir -p data
docker compose up -d
```

Voir les logs :
```bash
docker compose logs -f
```

Arrêter :
```bash
docker compose down
```

## Installation (Python natif)

```bash
pip install -r requirements.txt
cp .env.example .env
# Éditer .env avec tes infos
```

## Configuration rapide (sans KYC)

Exchanges recommandés sans KYC (jusqu'à ~1000€/j) :
- **MEXC** — très accessible, no-KYC basique disponible
- **Gate.io** — limites sans KYC acceptables
- **Bitget** — frais compétitifs

1. Créer un compte sur l'exchange choisi (email suffit pour les petits montants)
2. Déposer du BTC ou USDT
3. Créer une clé API (permissions : spot trading uniquement, PAS de retrait)
4. Remplir `.env`

## Démarrage (Python natif)

```bash
# Mode test (PAPER TRADING — aucun argent réel) :
PAPER_TRADING=true python grid_bot.py

# Mode réel :
PAPER_TRADING=false python grid_bot.py
```

## Paramètres clés (.env)

| Paramètre | Valeur conseillée | Description |
|---|---|---|
| `GRID_LEVELS` | 8-12 | Niveaux de la grille |
| `GRID_SPREAD` | 0.004-0.008 | Écart entre niveaux (0.5-0.8%) |
| `PRICE_RANGE_PCT` | 0.05 | Recentre si prix sort de ±5% |
| `STOP_LOSS_PCT` | 0.08 | Stop global à -8% du capital |
| `CAPITAL` | 80 | Capital en USDT |

## Notifications Discord

Ajoute ton webhook Discord dans `.env` (DISCORD_WEBHOOK) pour recevoir les rapports automatiques.

## Résultats attendus

Sur un marché volatile (BTC typiquement) :
- Spread 0.5% × quelques trades/jour = **2-8%/mois** réaliste
- Stop loss à -8% protège le capital

## ⚠️ Avertissement

Le trading comporte des risques. Ne pas investir plus que ce qu'on peut se permettre de perdre. Ce bot est fourni à titre éducatif.
