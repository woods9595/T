#!/usr/bin/env python3
"""Scanner d'engagement social LunarCrush.

Objectif : reperer les cryptos du top N dont l'engagement social s'emballe
AVANT que le prix ne suive, pour entrer en debut de cycle.

Usage :
    export LUNARCRUSH_API_KEY="..."
    python3 lunarcrush/scan.py                 # scan du top 30
    python3 lunarcrush/scan.py --top 50        # top 50
    python3 lunarcrush/scan.py --cache-only    # relit le cache, 0 requete API
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://lunarcrush.com/api4/public"
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
SNAPSHOTS = ROOT / "snapshots"

# Le plan impose 10 req/min : on s'auto-limite avec une marge.
MIN_INTERVAL_S = 6.5

# UA explicite obligatoire : Cloudflare renvoie 403 sur le
# "Python-urllib/x.y" par defaut.
USER_AGENT = "lunarcrush-engagement-scan/1.0"

DAY_S = 86400

# Exclus du classement : leur prix ne suit pas de cycle propre, donc un pic
# d'engagement chez eux n'est jamais une opportunite d'entree.
#   - stablecoins : arrimes au dollar
#   - wrapped / liquid staking : arrimes a BTC ou ETH, dont ils ne sont qu'un
#     miroir (WSTETH, STETH, WETH... polluaient 5 des 6 signaux sans filtre)
EXCLUDED = {
    # stablecoins
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "PYUSD", "TUSD", "USDS",
    "BUSD", "USD1", "USDF", "RLUSD", "USDD", "USDG", "GUSD", "LUSD",
    # wrapped / liquid staking derivatives
    "WBTC", "CBBTC", "TBTC", "LBTC", "SOLVBTC", "BTCB",
    "WETH", "STETH", "WSTETH", "WEETH", "RETH", "EZETH", "RSETH", "CBETH",
    "WBETH", "METH", "OSETH", "SWETH", "ANKRETH",
    "WBNB", "BSC-USD", "JITOSOL", "MSOL", "BNSOL", "JUPSOL",
}

_last_call = [0.0]


def _throttle():
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def fetch(path, key, retries=4):
    """GET sur l'API v4, avec throttling et backoff exponentiel sur 429/5xx."""
    url = f"{API}/{path}"
    for attempt in range(retries):
        _throttle()
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 402:
                raise SystemExit(f"[402] Endpoint hors abonnement : {path}")
            if e.code == 401:
                raise SystemExit("[401] Cle API invalide ou revoquee.")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                backoff = 2 ** (attempt + 1)
                print(f"  HTTP {e.code} sur {path}, retry dans {backoff}s...", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise SystemExit(f"Erreur reseau sur {path} : {e}")
    raise SystemExit(f"Echec apres {retries} tentatives : {path}")


def split_complete_days(rows, now_ts):
    """Separe les jours pleins du bucket du jour en cours.

    L'API renvoie toujours un dernier bucket partiel (le jour courant a
    l'heure qu'il est). L'inclure ecrasait mecaniquement le dernier point :
    a 11h UTC il ne contient que ~45% de la journee.
    """
    rows = [r for r in rows if r.get("time") and r.get("interactions") is not None]
    rows.sort(key=lambda r: r["time"])
    if rows and now_ts < rows[-1]["time"] + DAY_S:
        return rows[:-1], rows[-1]
    return rows, None


def robust_z(value, history):
    """Z-score robuste (mediane + MAD) : insensible aux pics isoles du passe."""
    if len(history) < 5:
        return 0.0
    med = statistics.median(history)
    mad = statistics.median([abs(x - med) for x in history])
    if mad == 0:
        return 0.0
    return 0.6745 * (value - med) / mad


def pct(new, old):
    return ((new - old) / old * 100) if old else 0.0


def sparkline(values):
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return blocks[0] * len(values)
    return "".join(blocks[min(7, int((v - lo) / (hi - lo) * 7.99))] for v in values)


def analyse(coin, rows, now_ts):
    """Compare l'engagement recent (24/48h) a une base de reference saine.

    La base exclut les 7 derniers jours : sinon une montee en cours gonflerait
    sa propre reference et le signal s'auto-annulerait.
    """
    symbol = coin["symbol"].upper()
    full, partial = split_complete_days(rows, now_ts)
    if len(full) < 14:
        return None

    def col(name):
        return [float(r.get(name) or 0) for r in full]

    inter, contrib = col("interactions"), col("contributors_active")
    posts, spam, close = col("posts_created"), col("spam"), col("close")

    base_int = [v for v in inter[-30:-7] if v > 0]
    base_con = [v for v in contrib[-30:-7] if v > 0]
    if len(base_int) < 5 or not base_con:
        return None

    med_int = statistics.median(base_int)
    med_con = statistics.median(base_con)
    if med_int <= 0 or med_con <= 0:
        return None

    surge_24h = inter[-1] / med_int
    surge_48h = statistics.mean(inter[-2:]) / med_int
    # La largeur d'audience (comptes uniques) est bien plus dure a manipuler
    # que le volume brut d'interactions : c'est le garde-fou anti-bot.
    breadth = statistics.mean(contrib[-2:]) / med_con
    z_int = robust_z(inter[-1], base_int)
    # Acceleration : le dernier jour plein compare aux 3 precedents, pour
    # attraper l'inflexion plutot qu'un plateau deja haut.
    prior3 = statistics.mean(inter[-4:-1]) if len(inter) >= 4 else med_int
    accel = inter[-1] / prior3 if prior3 else 1.0

    recent_posts = sum(posts[-3:])
    spam_share = min(sum(spam[-3:]) / recent_posts, 1.0) if recent_posts else 0.0

    # Le jour en cours, extrapole au prorata des heures ecoulees : indicatif
    # mais c'est le seul point qui capte un emballement de ce matin.
    pace = None
    if partial:
        elapsed = max(now_ts - partial["time"], 3600)
        if elapsed < DAY_S:
            projected = float(partial.get("interactions") or 0) * DAY_S / elapsed
            pace = round(projected / med_int, 2)

    # Prix : on prend le temps reel du listing plutot que les cloture
    # journalieres, qui accusent jusqu'a 24h de retard.
    chg_24h = float(coin.get("percent_change_24h") or 0)
    chg_7d = float(coin.get("percent_change_7d") or 0)
    chg_48h = pct(close[-1], close[-3]) if len(close) > 2 else 0.0

    spam_penalty = 1.0 - min(spam_share, 0.6)
    raw = (
        0.40 * min(surge_48h, 5.0) / 5.0
        + 0.30 * min(breadth, 5.0) / 5.0
        + 0.15 * min(max(z_int, 0.0), 8.0) / 8.0
        + 0.15 * min(max(accel - 1.0, 0.0), 2.0) / 2.0
    )
    score = round(raw * 100 * spam_penalty, 1)

    # Le stade compte autant que le score : un emballement social deja paye
    # par le prix n'est plus une entree en debut de cycle.
    hot = surge_48h >= 1.5 and breadth >= 1.2
    if not hot:
        stage = "CALME"
    elif chg_7d > 30 or chg_24h > 15:
        stage = "TARDIF"
    elif chg_24h >= 5 or chg_48h >= 8:
        stage = "EN COURS"
    else:
        stage = "PRECOCE"

    return {
        "symbol": symbol,
        "name": coin.get("name", symbol),
        "market_cap_rank": coin.get("market_cap_rank"),
        "score": score,
        "stage": stage,
        "surge_24h": round(surge_24h, 2),
        "surge_48h": round(surge_48h, 2),
        "pace_today": pace,
        "breadth": round(breadth, 2),
        "accel": round(accel, 2),
        "z_interactions": round(z_int, 1),
        "spam_share": round(spam_share, 3),
        "interactions_last_full_day": int(inter[-1]),
        "baseline_interactions": int(med_int),
        "last_full_day_utc": datetime.fromtimestamp(full[-1]["time"], timezone.utc).strftime("%Y-%m-%d"),
        "price": coin.get("price"),
        "chg_24h": round(chg_24h, 2),
        "chg_48h": round(chg_48h, 2),
        "chg_7d": round(chg_7d, 2),
        "engagement_daily": [int(v) for v in inter[-14:]],
        "sparkline": sparkline(inter[-14:]),
    }


def main():
    ap = argparse.ArgumentParser(description="Scanner d'engagement social LunarCrush")
    ap.add_argument("--top", type=int, default=30, help="taille du classement (defaut 30)")
    ap.add_argument("--no-filter", action="store_true", help="garder stables et tokens wrappes")
    ap.add_argument("--cache-only", action="store_true", help="relire le cache sans appeler l'API")
    ap.add_argument("--min-score", type=float, default=0.0, help="n'afficher qu'au-dessus de ce score")
    args = ap.parse_args()

    key = os.environ.get("LUNARCRUSH_API_KEY")
    if not key and not args.cache_only:
        raise SystemExit("LUNARCRUSH_API_KEY manquante. export LUNARCRUSH_API_KEY=...")

    CACHE.mkdir(exist_ok=True)
    SNAPSHOTS.mkdir(exist_ok=True)
    now_ts = int(time.time())

    if args.cache_only:
        coins = json.loads((CACHE / "_list.json").read_text())
    else:
        # On demande large puis on filtre : stables et wrapped occupent une
        # bonne partie du top et evinceraient de vraies cryptos du classement.
        listing = fetch(f"coins/list/v1?sort=market_cap&limit={args.top * 2 + 20}", key)
        coins = listing["data"]
        (CACHE / "_list.json").write_text(json.dumps(coins))

    if not args.no_filter:
        coins = [c for c in coins if c["symbol"].upper() not in EXCLUDED]
    coins = coins[: args.top]

    print(f"Scan de {len(coins)} cryptos ({'cache' if args.cache_only else 'API'})...", file=sys.stderr)

    results = []
    for i, coin in enumerate(coins, 1):
        sym = coin["symbol"].upper()
        path = CACHE / f"{sym}.json"
        if args.cache_only:
            if not path.exists():
                continue
            rows = json.loads(path.read_text())
        else:
            print(f"  [{i}/{len(coins)}] {sym}", file=sys.stderr)
            try:
                rows = fetch(f"coins/{sym}/time-series/v2?bucket=day&interval=1m", key)["data"]
            except urllib.error.HTTPError as e:
                print(f"    ignore ({e.code})", file=sys.stderr)
                continue
            path.write_text(json.dumps(rows))
        res = analyse(coin, rows, now_ts)
        if res:
            results.append(res)

    results.sort(key=lambda r: r["score"], reverse=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = SNAPSHOTS / f"{stamp}.json"
    snap.write_text(json.dumps(
        {"generated_utc": datetime.now(timezone.utc).isoformat(), "results": results},
        indent=2,
    ))

    order = {"PRECOCE": 0, "EN COURS": 1, "TARDIF": 2, "CALME": 3}
    shown = [r for r in results if r["score"] >= args.min_score]
    ref = results[0]["last_full_day_utc"] if results else "?"

    print(f"\nDernier jour plein : {ref} UTC   (x48h = engagement vs base 30j, "
          f"LARG = audience unique, AUJ = jour en cours extrapole)")
    print(f"\n{'SYM':<7}{'SCORE':>6}  {'STADE':<9}{'x48h':>6}{'AUJ':>6}{'LARG':>6}"
          f"{'SPAM':>6}{'24H%':>8}{'7J%':>8}  ENGAGEMENT 14 JOURS PLEINS")
    print("-" * 96)
    for r in sorted(shown, key=lambda r: (order[r["stage"]], -r["score"])):
        pace = f"{r['pace_today']:>6.1f}" if r["pace_today"] is not None else "     -"
        print(f"{r['symbol']:<7}{r['score']:>6.1f}  {r['stage']:<9}"
              f"{r['surge_48h']:>6.2f}{pace}{r['breadth']:>6.2f}"
              f"{r['spam_share']*100:>5.0f}%{r['chg_24h']:>8.1f}{r['chg_7d']:>8.1f}  {r['sparkline']}")

    for label, title in (("PRECOCE", "emballement social, prix pas encore parti"),
                         ("EN COURS", "le prix commence a suivre")):
        sel = [r for r in results if r["stage"] == label]
        print(f"\n{len(sel)} signal(s) {label} ({title}) :")
        for r in sel:
            print(f"  {r['symbol']:<6} engagement x{r['surge_48h']:.1f} "
                  f"({r['baseline_interactions']:,} -> {r['interactions_last_full_day']:,}/j), "
                  f"audience x{r['breadth']:.1f}, accel x{r['accel']:.1f}, "
                  f"prix 24h {r['chg_24h']:+.1f}% / 7j {r['chg_7d']:+.1f}%")
        if not sel:
            print("  aucun.")
    print(f"\nSnapshot : {snap}", file=sys.stderr)


if __name__ == "__main__":
    main()
