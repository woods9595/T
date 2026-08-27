#!/usr/bin/env python3
"""Detecteur d'allumage de tendance (donnees horaires LunarCrush).

Cherche le moment ou une crypto sort d'une phase calme : l'audience unique
s'elargit, de nouveaux posts apparaissent et le volume confirme -- avant que
le prix n'ait bouge.

Contrairement a scan.py, qui classe par intensite d'engagement, ce module
cherche l'INFLEXION depuis un plancher. Sur les cas etudies (ETHFI, ZEC,
HYPE), le pic d'engagement maximal coincidait avec le sommet du prix : le
niveau absolu est un signal de sortie, pas d'entree.

Usage :
    export LUNARCRUSH_API_KEY="..."
    python3 lunarcrush/ignition.py --top 15 --min-price 0.1 --max-price 3
    python3 lunarcrush/ignition.py --backtest --top 15 --min-price 0.1 --max-price 3
"""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scan import EXCLUDED, fetch  # noqa: E402

ROOT = Path(__file__).resolve().parent
HCACHE = ROOT / "cache_hourly"
SNAPSHOTS = ROOT / "snapshots"

H = 3600
LONG_BASE = 720     # reference sur 30j, arretee 72h avant : plus stable
                    # qu'une base 7j, que la montee en cours contamine vite
BASE_GAP = 72
MIN_HISTORY = 336   # 14j d'historique avant de pouvoir juger

# Seuils d'allumage, verifiables par --backtest.
T_BREADTH = 1.50      # audience unique (moyenne 24h) vs base 30j
T_ACCEL_FAST = 1.30   # depart brutal : 6h vs les 12h precedentes
T_ACCEL_SLOW = 1.40   # montee progressive : 24h vs les 72h precedentes
T_VOLUME = 1.40       # volume 24h vs base
T_COMPRESSION = 1.00  # l'audience doit etre passee par un plancher recemment
T_ACTIVE = 1.30       # et ne pas etre deja en train de retomber
COOLDOWN_H = 72       # un episode ne se recompte pas pendant 3 jours


def med(xs, default=0.0):
    xs = [x for x in xs if x and x > 0]
    return statistics.median(xs) if xs else default


def series(rows, field):
    return [float(r.get(field) or 0) for r in rows]


def features(rows, i):
    """Etat d'allumage a l'heure i. None si l'historique est insuffisant.

    Deux formes de depart coexistent et un seul test ne les attrape pas :
    ETHFI et HYPE demarrent par un a-coup de quelques heures, ZEC monte
    regulierement sur cinq jours. Un critere d'acceleration horaire ne voit
    que le premier ; on teste donc les deux echelles et on retient l'une ou
    l'autre.
    """
    if i < MIN_HISTORY:
        return None

    con = series(rows, "contributors_active")
    inter = series(rows, "interactions")
    posts = series(rows, "posts_created")
    vol = series(rows, "volume_24h")

    lo = max(0, i - LONG_BASE)
    base_con = med(con[lo:i - BASE_GAP])
    base_int = med(inter[lo:i - BASE_GAP])
    base_posts = med(posts[lo:i - BASE_GAP])
    base_vol = med(vol[lo:i - BASE_GAP])
    if base_con <= 0 or base_vol <= 0:
        return None

    mean24 = statistics.mean(con[i - 23:i + 1])
    mean6 = statistics.mean(con[i - 5:i + 1])
    breadth = mean24 / base_con
    active = mean6 / base_con
    accel_fast = mean6 / statistics.mean(con[i - 17:i - 5])
    accel_slow = mean24 / statistics.mean(con[i - 95:i - 23])

    vol_ratio = vol[i] / base_vol
    posts_ratio = (statistics.mean(posts[i - 23:i + 1]) / base_posts) if base_posts > 0 else 0.0
    int_ratio = statistics.mean(inter[i - 23:i + 1]) / base_int if base_int > 0 else 0.0

    # Compression : sur les 7 jours qui precedent, l'audience doit etre
    # redescendue au niveau de sa base. Sans ce plancher il n'y a pas de
    # ressort, seulement un plateau deja haut.
    prior = [statistics.mean(con[j - 23:j + 1]) / base_con
             for j in range(max(i - 168, 47), i - 23, 6)]
    compression = min(prior) if prior else 99.0

    fired = (
        breadth >= T_BREADTH
        and (accel_fast >= T_ACCEL_FAST or accel_slow >= T_ACCEL_SLOW)
        and vol_ratio >= T_VOLUME
        and compression <= T_COMPRESSION
        and active >= T_ACTIVE
    )

    return {
        "time": rows[i]["time"],
        "breadth": round(breadth, 2),
        "accel_fast": round(accel_fast, 2),
        "accel_slow": round(accel_slow, 2),
        "active": round(active, 2),
        "volume_ratio": round(vol_ratio, 2),
        "posts_ratio": round(posts_ratio, 2),
        "interactions_ratio": round(int_ratio, 2),
        "compression": round(compression, 2),
        "sentiment": int(rows[i].get("sentiment") or 0),
        "close": float(rows[i].get("close") or 0),
        "fired": fired,
    }


def is_pegged(rows, tolerance=1.04):
    """Vrai si le prix n'a pas d'amplitude sur 30 jours.

    Les stablecoins a rendement (SUSDE, USDY...) passent tous les filtres
    sociaux et rapportent structurellement 0 % : ils representaient la moitie
    des declenchements du premier backtest et ecrasaient les statistiques.
    Un test d'amplitude les ecarte sans avoir a les nommer un par un.

    Le seuil est calibre sur la separation observee : les actifs arrimes
    plafonnent a 2,4 % d'amplitude sur 30 jours, la premiere vraie crypto
    (TRX) est a 7,2 %. A 8 % le filtre excluait TRX a tort.
    """
    closes = [float(r.get("close") or 0) for r in rows if r.get("close")]
    if len(closes) < 100:
        return False
    lo, hi = min(closes), max(closes)
    return lo > 0 and hi / lo < tolerance


def fwd_return(rows, i, hours):
    j = i + hours
    if j >= len(rows):
        return None
    a = float(rows[i].get("close") or 0)
    b = float(rows[j].get("close") or 0)
    return ((b - a) / a * 100) if a > 0 else None


def max_fwd(rows, i, hours):
    """Meilleur gain atteignable sur la fenetre : ce qu'un trade bien sorti
    aurait pu capter, a distinguer du rendement a echeance fixe."""
    a = float(rows[i].get("close") or 0)
    window = rows[i + 1:i + 1 + hours]
    if not window or a <= 0:
        return None
    return (max(float(r.get("high") or r.get("close") or 0) for r in window) - a) / a * 100


def load_hourly(sym, key, cache_only=False):
    HCACHE.mkdir(exist_ok=True)
    path = HCACHE / f"{sym}.json"
    if cache_only or path.exists():
        if path.exists():
            return json.loads(path.read_text())
        return None
    rows = fetch(f"coins/{sym}/time-series/v2?bucket=hour&interval=1m", key)["data"]
    rows = [r for r in rows if r.get("time")]
    rows.sort(key=lambda r: r["time"])
    path.write_text(json.dumps(rows))
    return rows


def select_coins(args, key):
    depth = 1000 if (args.min_price or args.max_price) else args.top * 2 + 20
    listing = fetch(f"coins/list/v1?sort=market_cap&limit={depth}", key)["data"]
    coins = [c for c in listing if c["symbol"].upper() not in EXCLUDED]
    if args.min_price is not None:
        coins = [c for c in coins if (c.get("price") or 0) >= args.min_price]
    if args.max_price is not None:
        coins = [c for c in coins if (c.get("price") or 0) <= args.max_price]
    band = len(coins)
    coins = coins[: args.top]
    for sym in [s.strip().upper() for s in args.include.split(",") if s.strip()]:
        if sym not in {c["symbol"].upper() for c in coins}:
            match = [c for c in listing if c["symbol"].upper() == sym]
            if match:
                coins.append(match[0])
    return coins, band


def run_backtest(coins, key, args):
    """Rejoue le detecteur sur les 30 jours d'historique horaire.

    Mesure ce qu'aucune inspection de cas gagnants ne peut donner : le taux
    de faux positifs.
    """
    episodes = []
    for n, coin in enumerate(coins, 1):
        sym = coin["symbol"].upper()
        print(f"  [{n}/{len(coins)}] {sym}", file=sys.stderr)
        rows = load_hourly(sym, key, args.cache_only)
        if not rows or len(rows) < MIN_HISTORY + 24:
            continue
        if is_pegged(rows):
            print(f"    {sym} ignore : prix arrime (amplitude 30j < 4%)", file=sys.stderr)
            continue
        last_fire = -10 ** 9
        for i in range(MIN_HISTORY, len(rows)):
            f = features(rows, i)
            if not f or not f["fired"]:
                continue
            if rows[i]["time"] - last_fire < COOLDOWN_H * H:
                continue
            last_fire = rows[i]["time"]
            ep = dict(f, symbol=sym)
            for h, lbl in ((24, "r24h"), (72, "r72h"), (168, "r168h")):
                ep[lbl] = fwd_return(rows, i, h)
                ep["max_" + lbl] = max_fwd(rows, i, h)
            episodes.append(ep)
    return episodes


def main():
    ap = argparse.ArgumentParser(description="Detecteur d'allumage de tendance")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--max-price", type=float)
    ap.add_argument("--include", default="")
    ap.add_argument("--backtest", action="store_true", help="rejouer sur 30j et mesurer les faux positifs")
    ap.add_argument("--cache-only", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("LUNARCRUSH_API_KEY")
    if not key and not args.cache_only:
        raise SystemExit("LUNARCRUSH_API_KEY manquante.")

    coins, band = select_coins(args, key)
    if args.min_price or args.max_price:
        print(f"Bande {args.min_price}-{args.max_price}$ : {band} cryptos, "
              f"on garde les {len(coins)} plus grosses capitalisations.", file=sys.stderr)

    if args.backtest:
        eps = run_backtest(coins, key, args)
        print(f"\n{'='*94}\nBACKTEST — {len(eps)} allumage(s) sur 30 jours, {len(coins)} cryptos\n{'='*94}")
        if not eps:
            print("Aucun declenchement : seuils trop stricts, ou periode sans depart de tendance.")
            return
        print(f"\n{'SYM':<8}{'date UTC':<13}{'LARG':>6}{'ACCr':>6}{'ACCl':>6}{'VOL':>6}{'COMP':>6}"
              f"{'+24h':>8}{'+72h':>8}{'+7j':>8}{'max7j':>8}")
        print("-" * 100)
        for e in sorted(eps, key=lambda e: e["time"]):
            d = datetime.fromtimestamp(e["time"], timezone.utc)
            def f(v): return f"{v:>+8.1f}" if v is not None else "       -"
            print(f"{e['symbol']:<8}{d:%m-%d %H:%M}  {e['breadth']:>6.2f}{e['accel_fast']:>6.2f}"
                  f"{e['accel_slow']:>6.2f}{e['volume_ratio']:>6.2f}{e['compression']:>6.2f}"
                  f"{f(e['r24h'])}{f(e['r72h'])}{f(e['r168h'])}{f(e['max_r168h'])}")
        for lbl, k in (("+24h", "r24h"), ("+72h", "r72h"), ("+7j", "r168h")):
            vals = [e[k] for e in eps if e[k] is not None]
            if not vals:
                continue
            wins = sum(1 for v in vals if v > 0)
            big = sum(1 for v in vals if v > 10)
            print(f"\n{lbl:<5} n={len(vals):<3} positifs {wins}/{len(vals)} ({wins/len(vals)*100:.0f}%)  "
                  f">+10% : {big}  mediane {statistics.median(vals):+.1f}%  moyenne {statistics.mean(vals):+.1f}%")
        peaks = [e["max_r168h"] for e in eps if e["max_r168h"] is not None]
        if peaks:
            print(f"\nMeilleur point sur 7j : mediane {statistics.median(peaks):+.1f}%, "
                  f"max {max(peaks):+.1f}%")
        return

    # Etat du jour
    rows_now = []
    for n, coin in enumerate(coins, 1):
        sym = coin["symbol"].upper()
        print(f"  [{n}/{len(coins)}] {sym}", file=sys.stderr)
        rows = load_hourly(sym, key, args.cache_only)
        if not rows or len(rows) < MIN_HISTORY:
            continue
        if is_pegged(rows):
            print(f"    {sym} ignore : prix arrime (amplitude 30j < 4%)", file=sys.stderr)
            continue
        f = features(rows, len(rows) - 1)
        if not f:
            continue
        # Un allumage recent compte encore : on regarde les 24 dernieres heures.
        recent_fire = None
        for i in range(len(rows) - 1, max(len(rows) - 25, MIN_HISTORY), -1):
            g = features(rows, i)
            if g and g["fired"]:
                recent_fire = g
                break
        rows_now.append(dict(f, symbol=sym, name=coin.get("name", sym),
                             price=coin.get("price"),
                             chg_24h=coin.get("percent_change_24h"),
                             chg_7d=coin.get("percent_change_7d"),
                             recent_fire=recent_fire))

    rows_now.sort(key=lambda r: (r["recent_fire"] is None, -(r["breadth"] * max(r["accel_fast"], r["accel_slow"]))))
    now = datetime.now(timezone.utc)
    print(f"\nEtat au {now:%Y-%m-%d %H:%M} UTC — LARG=audience vs base 7j, ACC=acceleration, "
          f"VOL=volume vs base, COMP=compression prealable (bas = ressort)")
    print(f"\n{'SYM':<8}{'LARG':>6}{'ACCr':>6}{'ACCl':>6}{'VOL':>6}{'POSTS':>7}{'COMP':>6}"
          f"{'SENTI':>7}{'24H%':>7}{'7J%':>7}  ALLUMAGE")
    print("-" * 94)
    for r in rows_now:
        fire = "OUI " + datetime.fromtimestamp(r["recent_fire"]["time"], timezone.utc).strftime("%d/%m %Hh") \
            if r["recent_fire"] else "-"
        print(f"{r['symbol']:<8}{r['breadth']:>6.2f}{r['accel_fast']:>6.2f}{r['accel_slow']:>6.2f}"
              f"{r['volume_ratio']:>6.2f}{r['posts_ratio']:>7.2f}{r['compression']:>6.2f}"
              f"{r['sentiment']:>7}{float(r['chg_24h'] or 0):>7.1f}{float(r['chg_7d'] or 0):>7.1f}  {fire}")

    fired = [r for r in rows_now if r["recent_fire"]]
    print(f"\n{len(fired)} allumage(s) dans les 24 dernieres heures :")
    for r in fired:
        g = r["recent_fire"]
        d = datetime.fromtimestamp(g["time"], timezone.utc)
        print(f"  {r['symbol']:<7} {d:%d/%m %Hh} UTC — audience x{g['breadth']:.1f}, "
              f"accel x{max(g['accel_fast'], g['accel_slow']):.2f}, volume x{g['volume_ratio']:.1f}, "
              f"posts x{g['posts_ratio']:.1f}, sentiment {g['sentiment']}, "
              f"prix depuis : {(float(r['price'] or 0) - g['close']) / g['close'] * 100:+.1f}%")
    if not fired:
        print("  aucun. Les candidats les plus proches du seuil figurent en tete de tableau.")

    SNAPSHOTS.mkdir(exist_ok=True)
    out = SNAPSHOTS / f"ignition-{now:%Y-%m-%d}.json"
    out.write_text(json.dumps({"generated_utc": now.isoformat(), "coins": rows_now}, indent=2, default=str))
    print(f"\nSnapshot : {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
