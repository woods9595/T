# Scanner d'engagement social LunarCrush

Repere les cryptos du top N dont l'engagement social s'emballe **avant** que le
prix ne suive, pour entrer en debut de cycle plutot qu'apres la hausse.

## Installation

```bash
export LUNARCRUSH_API_KEY="ta_cle"     # https://lunarcrush.com/developers/keys
python3 lunarcrush/scan.py             # top 30, ~3,5 min
```

Aucune dependance externe : Python 3.9+ et la bibliotheque standard.

| Option | Effet |
|---|---|
| `--top N` | taille du classement (defaut 30) |
| `--min-price X --max-price Y` | ne garder que les cryptos dont le prix unitaire est dans cette bande |
| `--include SYM,SYM` | suivre ces symboles en plus, meme hors du top |
| `--scan-depth N` | profondeur du listing balaye avant filtrage (defaut 1000) |
| `--cache-only` | rejoue l'analyse sur le cache, **0 requete API** |
| `--min-score X` | masque les lignes sous ce score |
| `--no-filter` | garde les stablecoins et tokens wrappes |

## Lecture du tableau

| Colonne | Sens |
|---|---|
| `x24h` | engagement du dernier jour plein / mediane des 30 jours. **2,0 = deux fois le bruit de fond.** |
| `ACC` | acceleration : dernier jour plein / moyenne des 3 precedents. Au-dessus de 1, ca monte encore ; en dessous, ca retombe. |
| `AUJ` | jour en cours extrapole au prorata des heures ecoulees. Indicatif, mais c'est le seul point qui capte un emballement du matin meme. |
| `LARG` | largeur d'audience : comptes uniques qui postent, meme ratio. **Le garde-fou anti-bot** — du volume sans largeur, c'est de la manipulation. |
| `SPAM` | part de posts classes spam. Au-dela de ~50 %, le signal est douteux. |
| `SCORE` | 0-100, combine surge + largeur + z-score + acceleration, penalise par le spam. |

### Les stades

C'est la colonne qui repond a ta question. Un score eleve ne dit pas s'il est
encore temps d'entrer :

- **PRECOCE** — l'engagement s'emballe, le prix n'a pas bouge. C'est la fenetre.
- **EN COURS** — le prix commence a suivre (+5 % en 24 h ou +8 % en 48 h).
- **TARDIF** — deja +30 % sur 7 jours. Le mouvement est fait.
- **RETOMBE** — un pic a eu lieu ces 5 derniers jours mais l'engagement est
  redescendu. Le train est parti.
- **CALME** — rien qui se detache du bruit.

## Choix de methode

- **Base de reference excluant les 7 derniers jours.** Sinon une montee en
  cours gonfle sa propre reference et le signal s'auto-annule.
- **Mediane + MAD** plutot que moyenne + ecart-type : un pic isole dans le
  passe ne doit pas anesthesier le detecteur.
- **Le bucket du jour en cours est ecarte** des jours pleins. L'API renvoie
  toujours un dernier bucket partiel : a 11 h UTC il ne contient que ~45 % de
  la journee, et l'inclure ecrasait mecaniquement le dernier point.
- **Stables et tokens wrappes exclus** (`EXCLUDED` dans `scan.py`). WSTETH,
  STETH, WETH, CBBTC sont arrimes a ETH/BTC : sans ce filtre ils occupaient
  5 des 6 signaux, sans qu'aucun soit une opportunite reelle.
- **Prix temps reel** issu du listing, pas des clotures journalieres qui
  accusent jusqu'a 24 h de retard.
- **Le score porte sur le dernier jour plein, pas sur une moyenne 48 h.** Une
  moyenne reste haute pendant toute la retombee d'un pic : ETHFI, apres un
  sommet a x11,3 le 24/08 suivi d'un effondrement a x0,73 le 26, ressortait
  encore a "x1,67" et etait classe PRECOCE. C'est le stade RETOMBE qui isole
  desormais ce cas.

## Quotas

Le plan impose **10 requetes/minute et 2000/jour**. Le script s'auto-limite a
6,5 s entre appels ; un scan du top 30 coute 31 requetes (~3,5 min). Tu peux
donc lancer une soixantaine de scans par jour.

## Exemple : bande de prix

```bash
python3 lunarcrush/scan.py --top 10 --min-price 0.01 --max-price 2 --include ETHFI
```

Attention a ce que ce filtre selectionne : sur 605 cryptos cotees entre 0,01 $
et 2 $, les 10 plus grosses capitalisations sont XRP, TRX, DOGE, ADA, XLM...
c'est-a-dire des large caps qui bougent peu. Le prix unitaire ne dit rien de la
taille : ETHFI a 0,58 $ pese 2 000 fois moins que XRP a 1,42 $. Pour viser des
capitalisations comparables a ETHFI, filtrer le prix ne suffit pas.

## Historique

Chaque run ecrit `snapshots/AAAA-MM-JJ.json` (versionne) et met en cache les
series brutes dans `cache/` (ignore par git). Les series remontant 30 jours,
l'historique d'engagement jour par jour est disponible des le premier run.

Pour un scan quotidien automatique :

```cron
0 7 * * * cd /chemin/vers/T && LUNARCRUSH_API_KEY=... python3 lunarcrush/scan.py >> scan.log 2>&1
```

## Limites

Un pic d'engagement precede parfois une hausse, souvent rien, et parfois une
distribution organisee. Cet outil filtre le bruit et hierarchise l'attention —
il ne predit pas les prix et ne constitue pas un conseil en investissement.

---

# ignition.py — detecteur d'allumage (horaire)

`scan.py` classe par intensite d'engagement. Sur les cas etudies (ETHFI, ZEC,
HYPE), le pic d'engagement maximal **coincidait avec le sommet du prix** :
ETHFI a culmine a x11,3 le 24/08, jour exact du top, suivi de -7,9 %.
Le niveau absolu est donc un signal de sortie, pas d'entree.

`ignition.py` cherche l'inflexion depuis un plancher, en donnees horaires.

```bash
python3 lunarcrush/ignition.py --top 15 --min-price 0.1 --max-price 3
python3 lunarcrush/ignition.py --backtest --top 40 --min-price 0.1 --max-price 3
```

## Les cinq criteres

Un allumage est retenu quand les cinq conditions sont vraies ensemble :

| Critere | Seuil | Pourquoi |
|---|---|---|
| **LARG** audience unique 24h vs base 30j | ≥ 1,50 | `contributors_active` est bien plus dur a manipuler que les interactions. XMR : pic d'interactions x5,7 avec une audience a x1,16 — un post viral, pas un mouvement, et le prix n'a pas suivi. |
| **ACC** acceleration, rapide **ou** lente | ≥ 1,30 (6h/12h) ou ≥ 1,40 (24h/72h) | Deux formes de depart coexistent : ETHFI et HYPE demarrent par un a-coup de quelques heures, ZEC monte regulierement sur cinq jours. Un seul test ne voit que la premiere. |
| **VOL** volume 24h vs base | ≥ 1,40 | Social sans volume = bruit. C'est ce qui separe proprement les cas qui ont monte de ceux qui n'ont rien fait. |
| **COMP** plancher recent | ≤ 1,00 | Sans compression prealable, pas de ressort : seulement un plateau deja haut. |
| **ACTIF** audience 6h vs base | ≥ 1,30 | Evite de se declencher sur la retombee d'un pic. |

Le **sentiment** ne sert pas a entrer mais a sortir : celui d'ETHFI s'est
effondre de 83 a 46 exactement au sommet.

Les actifs arrimes sont ecartes automatiquement par l'amplitude de prix sur
30 jours (seuil 4 %) plutot que par une liste de symboles : les stablecoins a
rendement (SUSDE, USDY, USD0) passent tous les filtres sociaux et rapportent
structurellement 0 %. Ils representaient la moitie des declenchements du
premier backtest.

## Resultat du backtest — a lire avant toute utilisation

Sur 30 jours, 35 cryptos, 25 allumages :

| horizon | signal (mediane) | marche (mediane) | ecart | bat le marche |
|---|---|---|---|---|
| +24 h | +0,8 % | +0,3 % | +0,5 % | 13/25 (52 %) |
| +72 h | +2,0 % | +2,4 % | −0,4 % | 10/23 (43 %) |
| +7 j | +10,4 % | +9,9 % | +0,5 % | 6/12 (50 %) |

**Le detecteur n'a aucun avantage mesurable sur le marche.** Les +11,9 % de
rendement moyen a 7 jours, impressionnants isolement, sont integralement du
beta : l'univers entier a fait +9,9 % sur les memes fenetres. Le taux de
succes contre le marche est celui d'un tirage a pile ou face.

Ce que le backtest montre malgre tout, sur des echantillons trop petits pour
conclure : le **premier** allumage apres une phase calme fait +6,0 % median a
72 h (13/18 positifs) tandis qu'un re-allumage fait −1,6 % (1/4). Si une piste
merite d'etre creusee, c'est celle-la.

Limites : 30 jours d'historique, une seule phase de marche (haussiere),
n = 12 sur l'horizon 7 jours. Ces chiffres ne suffisent ni a valider ni a
rejeter definitivement la methode — ils suffisent en revanche a dire qu'elle
n'est pas prete a porter des decisions d'investissement.
