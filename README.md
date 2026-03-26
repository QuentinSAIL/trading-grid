# Grid Trading Bot

Strategie : **Grid Trading** — profite de la volatilite en achetant bas / vendant haut automatiquement.

## Comment ca marche

Le bot place une grille d'ordres buy et sell autour du prix actuel :
```
SELL @ 65975  <- niveau +3
SELL @ 65650  <- niveau +2
SELL @ 65325  <- niveau +1
--- PRIX ACTUEL : 65000 ---
BUY  @ 64675  <- niveau -1
BUY  @ 64350  <- niveau -2
BUY  @ 64025  <- niveau -3
```
A chaque oscillation du marche, des ordres se remplissent -> profit.

## Installation (Docker — recommande)

```bash
git clone git@github.com:QuentinSAIL/trading-grid.git
cd trading-grid
cp .env.example .env
# Editer .env avec tes infos
mkdir -p data
docker compose up -d
```

Voir les logs :
```bash
docker compose logs -f
```

Arreter :
```bash
docker compose down
```

## Installation (Python natif)

```bash
pip install -r requirements.txt
cp .env.example .env
# Editer .env avec tes infos
```

## Configuration rapide (sans KYC)

Exchanges recommandes sans KYC (jusqu'a ~1000EUR/j) :
- **MEXC** — tres accessible, no-KYC basique disponible
- **Gate.io** — limites sans KYC acceptables
- **Bitget** — frais competitifs

1. Creer un compte sur l'exchange choisi (email suffit pour les petits montants)
2. Deposer du BTC ou USDT
3. Creer une cle API (permissions : spot trading uniquement, PAS de retrait)
4. Remplir `.env`

## Demarrage (Python natif)

```bash
# Mode test (PAPER TRADING — aucun argent reel) :
PAPER_TRADING=true python grid_bot.py

# Mode reel :
PAPER_TRADING=false python grid_bot.py
```

## Dashboard CLI

Le dashboard affiche en temps reel l'etat du bot dans le terminal (auto-refresh toutes les 2s) :

```bash
# Depuis la meme machine que le bot :
python dashboard.py

# Avec un chemin custom vers le state :
python dashboard.py /app/data/bot_state.json

# Via Docker :
docker compose exec grid-bot python dashboard.py
```

Le dashboard affiche :
- **Header** : prix actuel, drift, profit, ROI, nombre de trades, uptime
- **Grille** : ordres SELL (rouge) et BUY (vert), contre-ordres en gras
- **Fills** : 15 derniers fills avec type (init/cycle) et profit
- **Solde** : USDT et asset avec repartition libre/en ordres/total + valeur en USDT

## Backtester

Teste la strategie sur des donnees historiques avant de risquer du capital :

```bash
# Backtester les 30 derniers jours avec la config .env :
python backtest.py 30

# Backtester 7 jours sur ETH :
python backtest.py 7 --symbol ETH/USDT

# Tester differents parametres :
python backtest.py 90 --spread 0.008 --levels 12 --capital 200

# Toutes les options :
python backtest.py --help
```

Options :
| Option | Defaut | Description |
|---|---|---|
| `days` | (requis) | Nombre de jours a backtester |
| `--symbol` | .env ou BTC/USDT | Paire de trading |
| `--exchange` | .env ou mexc | Exchange pour les donnees |
| `--capital` | .env ou 80 | Capital simule en USDT |
| `--levels` | .env ou 10 | Niveaux de grille |
| `--spread` | .env ou 0.005 | Ecart entre niveaux |
| `--fee` | 0.001 | Frais taker (0.1%) |
| `--timeframe` | 1h | Timeframe des bougies |
| `--range-pct` | .env ou 0.05 | Seuil de recentrage |
| `--stop-loss` | .env ou 0.08 | Stop loss global |

Le backtester affiche : profit net, ROI, ROI/jour, projection mensuelle, drawdown max, comparaison vs buy & hold, equity curve, et top 5 fills.

## Parametres cles (.env)

| Parametre | Valeur conseillee | Description |
|---|---|---|
| `GRID_LEVELS` | 8-12 | Niveaux de la grille |
| `GRID_SPREAD` | 0.004-0.008 | Ecart entre niveaux (0.5-0.8%) |
| `PRICE_RANGE_PCT` | 0.05 | Recentre si prix sort de +/-5% |
| `STOP_LOSS_PCT` | 0.08 | Stop global a -8% du capital |
| `CAPITAL` | 80 | Capital en USDT |
| `MAX_OPEN_ORDERS` | 20 | Nombre max d'ordres simultanes |

## Notifications Discord

Ajoute ton webhook Discord dans `.env` (DISCORD_WEBHOOK) pour recevoir les rapports automatiques.

## Resultats attendus

Sur un marche volatile (BTC typiquement) :
- Spread 0.5% x quelques trades/jour = **2-8%/mois** realiste
- Stop loss a -8% protege le capital
- Utilise le backtester pour valider avant de passer en live

## Avertissement

Le trading comporte des risques. Ne pas investir plus que ce qu'on peut se permettre de perdre. Ce bot est fourni a titre educatif.
