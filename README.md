# Grid Trading Bot — « Robust Harvester »

Bot de trading spot qui **récolte la volatilité** via une grille d'ordres, avec
une gestion de risque conçue et validée par backtest pour rester **rentable dans
tous les régimes de marché testés** (bull, bear, choppy).

## La stratégie en 30 secondes

Le bot place une grille d'ordres buy/sell autour du prix. À chaque oscillation,
des ordres se remplissent et des contre-ordres verrouillent un petit profit :

```
SELL @ 82320  ← +2
SELL @ 81510  ← +1
--- PRIX : 80700 ---
BUY  @ 79890  ← -1
BUY  @ 79080  ← -2
```

**Ce qui rend cette version rentable** (vs un grid naïf) :

1. **Sizing ÉGAL par niveau** (`WEIGHT_FACTOR=0`) — c'est le levier n°1. Un grid
   qui concentre le capital près du prix (ou en DCA aux extrêmes) sous-performe ;
   un harvester à poids égal capture mieux les allers-retours. Découvert et
   confirmé par sweep + validation cross-asset.
2. **Inventaire plafonné à 30%** (`MAX_INV_RATIO=0.30`) + rebalance dynamique
   piloté par la tendance → le bot reste **majoritairement en cash**, ce qui le
   protège pendant les bear markets (là où un grid classique accumule des sacs).
3. **Spread dynamique** (volatilité + Bollinger Bands) : la grille s'élargit
   quand ça bouge, se resserre quand c'est calme.
4. **RSI agressif, stale-order decay, stop-loss portefeuille** pour la robustesse.

### Résultats backtest (BTC/USDT 1h, MEXC, maker 0% / taker 0.1%)

| Fenêtre | ROI stratégie | Max DD | Buy & Hold |
|---|---|---|---|
| 90 j  | **+3.6 %** | 2.9 % | +27 % |
| 180 j | **+3.9 %** | 4.3 % | +15 % |
| 365 j | **+3.3 %** | 6.4 % | **−29 %** |
| 800 j | **+23.0 %** | 6.8 % | +41 % |

**Tout positif, drawdown ≤ 7 %.** Le point clé : sur l'année de bear (365 j où le
BTC fait **−29 %**), la stratégie reste **+3.3 %** — elle protège le capital et
récolte le chop au lieu de le subir. En bull (800 j) elle capte +23 % sans jamais
prendre plus de 7 % de drawdown, contre 54 % de drawdown pour le buy & hold.

> Reproduis-le toi-même : `python backtest.py 90`, `... 180`, `... 365`, `... 800`.

### Overlay tendance (optionnel, `--trend-overlay`)

Un sleeve trend-following (cassure Donchian 168h + stop suiveur ATR ×2.5) peut
être ajouté pour **capter davantage les gros bulls** :

| Fenêtre | Harvester seul | + overlay trend |
|---|---|---|
| 365 j | +3.3 % | +4.4 % |
| 800 j | +23.0 % | **+31.8 %** (Sharpe 1.3) |

`python backtest.py 800 --trend-overlay`

⚠️ L'overlay **augmente le rendement mais réduit la robustesse hors-échantillon**
(c'est un pari directionnel long : il gagne en tendance haussière, perd en
whipsaw). Désactivé par défaut. Ne l'active en live qu'après l'avoir validé
toi-même sur plusieurs périodes.

## Plus de rendement : le PORTEFEUILLE multi-actifs (recommandé)

Le meilleur levier de rendement n'est pas plus de risque directionnel, c'est la
**diversification**. Lancer le harvester sur plusieurs actifs décorrélés (chacun
tout-positif individuellement) **multiplie le rendement et le Sharpe** tout en
gardant un drawdown maîtrisé — les alts touchent leur point bas à des moments
différents, ce qui lisse la courbe.

Backtest 7 actifs (BTC, ETH, SOL, BNB, XRP, LINK, LTC), equal-weight, harvester,
frais réalistes :

| Portefeuille | 90j | 180j | 365j | 800j | Max DD | Sharpe |
|---|---|---|---|---|---|---|
| BTC seul | +4.6 % | +5.6 % | +5.3 % | +28.9 % | 6.9 % | 1.40 |
| 3 actifs (BTC,ETH,SOL) | +8.4 % | +6.7 % | +1.6 % | +34.0 % | 15.0 % | 1.57 |
| **7 actifs diversifiés** | **+8.5 %** | **+8.0 %** | **+6.9 %** | **+92.6 %** | **11.1 %** | **2.43** |

Le portefeuille 7-actifs est **tout-positif sur toutes les fenêtres** (y compris
le bear 365 j), avec un **Sharpe 2.4** (vs 1.4 en BTC seul) et un drawdown *plus
faible* que le portefeuille 3-actifs.

> ⚠️ Les +92 % sur 800 j sont gonflés par le bull des alts et les fills 1h
> optimistes — ne les prends pas au pied de la lettre. Le signal solide et
> défendable est : **tout-positif toutes fenêtres, Sharpe ~2.4, DD ~11 %**. Les
> 7 actifs sont choisis par **liquidité** (ex-ante), pas par performance, pour
> éviter le biais de sélection.

**Backtester le portefeuille :**
```bash
python backtest.py 800 --portfolio BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,LINK/USDT,LTC/USDT
```

**Déployer le portefeuille** (7 instances, 1 par actif, même compte) :
```bash
# ~25 USDT par instance (7 x 25 = 175 USDT). Ajuste FIXED_CAPITAL_EACH.
FIXED_CAPITAL_EACH=25 docker compose -f docker-compose.portfolio.yml up -d
```
Le mode `FIXED_CAPITAL` (budget fixe par bot) empêche les instances de se
disputer le solde USDT partagé. Pour un cloisonnement strict, utilise des
sous-comptes / clés API distinctes par actif.

## Honnêteté / limites (lis ça)

La stratégie a été soumise à une validation adverse (audit look-ahead, stress
frais/slippage, walk-forward out-of-sample, réalisme des fills intrabar). Verdicts :

- ✅ **Pas de look-ahead**, comptabilité des frais correcte (reproduit au centime).
- ✅ **Le harvester généralise** : tout-positif aussi sur ETH & SOL et sur MEXC
  sans re-tuning ; positif sur 3/5 blocs de 150 j non-chevauchants (pire bloc
  −0.6 %). C'est le composant le plus solide.
- ⚠️ **Sensible aux coûts** : l'edge est réel mais modeste. À frais MEXC (maker 0%,
  taker 0.1%) il est positif ; si tes frais réels montent (taker ≥ 0.15 % +
  slippage), le 365 j peut passer légèrement négatif. Utilise un exchange à maker
  0% (MEXC) et des ordres limit.
- ⚠️ **Fills 1h optimistes** : le backtest 1h suppose que tous les niveaux touchés
  dans une bougie se remplissent. En réel, attends-toi à un peu moins que le
  backtest. Le live tourne sur des ordres limit vérifiés toutes les 30 s.
- ⚠️ **Aucune stratégie spot long-only n'est garantie positive dans chaque bear.**
  Le harvester minimise l'exposition mais ne la supprime pas totalement.

**Conclusion honnête** : ce n'est pas une machine à imprimer de l'argent, c'est un
récolteur de volatilité à faible drawdown, rentable sur les données testées et
robuste cross-actifs, à condition de frais bas. Commence en `PAPER_TRADING=true`.

## Installation (Docker — recommandé)

```bash
git clone git@github.com:QuentinSAIL/trading-grid.git
cd trading-grid
cp .env.example .env
# Editer .env avec tes infos
mkdir -p data
docker compose up -d
docker compose logs -f
```

## Installation (Python natif)

```bash
pip install -r requirements.txt
cp .env.example .env
# Mode test (PAPER — aucun argent reel) :
PAPER_TRADING=true python grid_bot.py
# Mode reel :
PAPER_TRADING=false python grid_bot.py
```

## Backtester

```bash
python backtest.py 90                      # 90 derniers jours (config .env)
python backtest.py 365                      # l'annee de bear — le test qui compte
python backtest.py 800 --trend-overlay      # avec overlay tendance
python backtest.py 90 --symbol ETH/USDT     # autre paire
python backtest.py --help                   # toutes les options
```

Affiche : ROI, ROI/jour, projection mensuelle, drawdown max, Sharpe, comparaison
vs buy & hold, equity curve.

## Sweep (optimisation de paramètres)

```bash
python sweep.py 365     # teste des centaines de configs sur 365 jours
```

## Dashboard CLI

```bash
python dashboard.py                    # auto-refresh 2s
docker compose exec grid-bot python dashboard.py
```

## Paramètres clés (.env)

| Paramètre | Défaut | Description |
|---|---|---|
| `WEIGHT_FACTOR` | **0.0** | Sizing par niveau — 0 = égal (le levier clé) |
| `GRID_LEVELS` | 4 | Niveaux de chaque côté |
| `GRID_SPREAD` | 0.010 | Écart de base (adapté dynamiquement) |
| `MAX_INV_RATIO` | 0.30 | Cap inventaire BTC (protection bear) |
| `INV_TARGET` | 0.18 | Ratio d'inventaire cible |
| `PRICE_RANGE_PCT` | 0.04 | Recentrage si drift > 4 % |
| `STOP_LOSS_PCT` | 0.25 | Stop global à −25 % |
| `TREND_OVERLAY` | false | Active le sleeve trend (optionnel) |
| `MAKER_FEE` / `TAKER_FEE` | 0.0 / 0.001 | Frais MEXC |

## Recherche & validation

Le dossier `research/` contient le harnais de backtest hors-ligne (données OHLCV
en cache, moteur d'allocation vectorisé, comparateurs cross-asset et par blocs
non-chevauchants) utilisé pour concevoir et valider la stratégie.

## Avertissement

Le trading comporte des risques. Ne pas investir plus que ce qu'on peut se
permettre de perdre. Les performances passées (backtest) ne préjugent pas des
performances futures. Fourni à titre éducatif.
