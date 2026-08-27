#!/usr/bin/env python3
"""Backtest factoriel sur 22 mois d'historique journalier LunarCrush.

Le detecteur d'allumage (ignition.py) vise des mouvements de quelques jours et
n'a montre aucun avantage sur le marche. Ce module change d'echelle et de
methode :

- horizon 3 a 6 mois plutot que 24-72h ;
- classement TRANSVERSAL (chaque crypto comparee aux autres a la meme date)
  plutot que des seuils absolus, ce qui neutralise les phases de marche ;
- mesure du pouvoir predictif facteur par facteur (correlation de rang avec
  le rendement futur), au lieu d'un score compose invérifiable.

Usage :
    python3 lunarcrush/factors.py --fetch --universe 60   # collecte (long)
    python3 lunarcrush/factors.py --backtest
    python3 lunarcrush/factors.py --rank
"""

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from scan import EXCLUDED, fetch

ROOT = Path(__file__).resolve().parent
LCACHE = ROOT / "cache_long"
DAY = 86400

# 22 mois : assez pour couvrir plusieurs regimes, ce que 30 jours ne
# permettaient pas.
HISTORY_DAYS = 660


def load_long(sym, key, cache_only=False):
    LCACHE.mkdir(exist_ok=True)
    path = LCACHE / f"{sym}.json"
    if path.exists():
        return json.loads(path.read_text())
    if cache_only:
        return None
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - HISTORY_DAYS * DAY
    rows = fetch(f"coins/{sym}/time-series/v2?bucket=day&start={start}&end={end}", key)["data"]
    rows = [r for r in rows if r.get("time") and r.get("close")]
    rows.sort(key=lambda r: r["time"])
    path.write_text(json.dumps(rows))
    return rows


def mean_win(vals, a, b):
    """Moyenne de vals[a:b], en ignorant les trous."""
    xs = [v for v in vals[a:b] if v is not None and v > 0]
    return statistics.mean(xs) if xs else None


def compute_factors(rows, i):
    """Facteurs a la date i, calcules uniquement sur le passe.

    Toute fenetre se termine a i inclus : aucune donnee posterieure n'entre
    dans le calcul, sinon le backtest se regarde lui-meme.
    """
    if i < 370:
        return None
    close = [float(r.get("close") or 0) for r in rows]
    con = [float(r.get("contributors_active") or 0) for r in rows]
    sdom = [float(r.get("social_dominance") or 0) for r in rows]
    mdom = [float(r.get("market_dominance") or 0) for r in rows]
    inter = [float(r.get("interactions") or 0) for r in rows]
    senti = [float(r.get("sentiment") or 0) for r in rows]
    gal = [float(r.get("galaxy_score") or 0) for r in rows]
    vol = [float(r.get("volume_24h") or 0) for r in rows]

    p = close[i]
    if p <= 0:
        return None

    f = {}
    f["mom_6m"] = (p / close[i - 180] - 1) if close[i - 180] > 0 else None
    f["mom_3m"] = (p / close[i - 90] - 1) if close[i - 90] > 0 else None
    f["mom_1m"] = (p / close[i - 30] - 1) if close[i - 30] > 0 else None

    hi = max(close[i - 360:i + 1])
    f["drawdown_12m"] = (p / hi - 1) if hi > 0 else None

    # Croissance de la base d'audience : 30 derniers jours contre les 90 qui
    # precedent. C'est la version longue du critere qui portait le detecteur
    # horaire, la ou il avait au moins une justification empirique.
    a, b = mean_win(con, i - 29, i + 1), mean_win(con, i - 119, i - 29)
    f["contrib_growth"] = (a / b - 1) if a and b else None

    a, b = mean_win(inter, i - 29, i + 1), mean_win(inter, i - 119, i - 29)
    f["interactions_growth"] = (a / b - 1) if a and b else None

    # Attention rapportee a la taille : une dominance sociale qui progresse
    # plus vite que la dominance de marche signale une attention
    # disproportionnee a la capitalisation. Mesure transversale par
    # construction, donc insensible a l'inflation generale d'attention qui
    # faussait les ratios absolus.
    a, b = mean_win(sdom, i - 29, i + 1), mean_win(sdom, i - 119, i - 29)
    f["social_dom_growth"] = (a / b - 1) if a and b else None
    sd, md = mean_win(sdom, i - 29, i + 1), mean_win(mdom, i - 29, i + 1)
    f["attention_ratio"] = (sd / md) if sd and md else None

    f["sentiment"] = mean_win(senti, i - 29, i + 1)
    f["galaxy"] = mean_win(gal, i - 29, i + 1)
    a, b = mean_win(vol, i - 29, i + 1), mean_win(vol, i - 119, i - 29)
    f["volume_growth"] = (a / b - 1) if a and b else None
    return f


def spearman(xs, ys):
    """Correlation de rang. Robuste aux distributions tres asymetriques des
    rendements crypto, contrairement a Pearson."""
    n = len(xs)
    if n < 8:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda k: v[k])
        r = [0.0] * n
        k = 0
        while k < n:
            j = k
            while j + 1 < n and v[order[j + 1]] == v[order[k]]:
                j += 1
            avg = (k + j) / 2 + 1
            for m in range(k, j + 1):
                r[order[m]] = avg
            k = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


FACTORS = ["mom_6m", "mom_3m", "mom_1m", "drawdown_12m", "contrib_growth",
           "interactions_growth", "social_dom_growth", "attention_ratio",
           "sentiment", "galaxy", "volume_growth"]


def build_universe(args, key):
    listing = fetch("coins/list/v1?sort=market_cap&limit=300", key)["data"]
    out, seen = [], set()
    for c in listing:
        s = c["symbol"].upper()
        if s in EXCLUDED or s in seen:
            continue
        if (c.get("market_cap") or 0) < 100e6:
            continue
        seen.add(s)
        out.append(c)
    return out[: args.universe]


def is_pegged(rows, tol=1.04):
    cl = [float(r.get("close") or 0) for r in rows if r.get("close")]
    if len(cl) < 200:
        return False
    lo, hi = min(cl), max(cl)
    return lo > 0 and hi / lo < tol


def panel(cache_only=True):
    """Charge toutes les series en cache et indexe par date."""
    data = {}
    for path in sorted(LCACHE.glob("*.json")):
        sym = path.stem
        rows = json.loads(path.read_text())
        if len(rows) < 400 or is_pegged(rows):
            continue
        data[sym] = rows
    return data


def run_backtest(data, horizons=(90, 180), step=15):
    """Mesure, date par date, la correlation de rang entre chaque facteur et
    le rendement futur transversal.

    L'information coefficient (IC) moyen est la mesure standard : positif et
    stable = le facteur ordonne correctement les cryptos. Autour de zero = le
    facteur n'apporte rien, quel que soit l'aspect du backtest en cumul.
    """
    idx = {s: {r["time"]: k for k, r in enumerate(rows)} for s, rows in data.items()}
    dates = sorted(set.intersection(*[set(idx[s]) for s in data]) if data else [])
    if not dates:
        return {}, []
    usable = [t for t in dates if all(idx[s][t] >= 370 for s in data)]
    sample = usable[::step]

    results = {h: {f: [] for f in FACTORS} for h in horizons}
    coverage = []
    for t in sample:
        feats, fwd = {}, {}
        for s, rows in data.items():
            i = idx[s][t]
            f = compute_factors(rows, i)
            if not f:
                continue
            feats[s] = f
            fwd[s] = {}
            for h in horizons:
                j = i + h
                if j < len(rows):
                    a, b = float(rows[i]["close"]), float(rows[j]["close"])
                    fwd[s][h] = (b / a - 1) if a > 0 else None
        for h in horizons:
            syms = [s for s in feats if fwd[s].get(h) is not None]
            if len(syms) < 15:
                continue
            for fac in FACTORS:
                pairs = [(feats[s][fac], fwd[s][h]) for s in syms if feats[s].get(fac) is not None]
                if len(pairs) < 15:
                    continue
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is not None:
                    results[h][fac].append(ic)
            coverage.append((t, h, len(syms)))
    return results, coverage


def decile_test(data, factor, horizon=180, step=15, top_n=8):
    """Rendement du groupe de tete contre la mediane de l'univers.

    L'IC dit si le facteur ordonne ; ceci dit ce que la strategie aurait
    rapporte en pratique, ecart au marche compris.
    """
    idx = {s: {r["time"]: k for k, r in enumerate(rows)} for s, rows in data.items()}
    dates = sorted(set.intersection(*[set(idx[s]) for s in data]))
    usable = [t for t in dates if all(idx[s][t] >= 370 for s in data)][::step]
    rows_out = []
    for t in usable:
        vals, fwd = {}, {}
        for s, rws in data.items():
            i = idx[s][t]
            f = compute_factors(rws, i)
            j = i + horizon
            if not f or f.get(factor) is None or j >= len(rws):
                continue
            a, b = float(rws[i]["close"]), float(rws[j]["close"])
            if a <= 0:
                continue
            vals[s] = f[factor]
            fwd[s] = b / a - 1
        if len(vals) < 15:
            continue
        top = sorted(vals, key=lambda s: -vals[s])[:top_n]
        rows_out.append((t, statistics.median([fwd[s] for s in top]),
                         statistics.median(list(fwd.values())),
                         sum(1 for s in top if fwd[s] > 0.5) / len(top)))
    return rows_out


def main():
    ap = argparse.ArgumentParser(description="Backtest factoriel long terme")
    ap.add_argument("--fetch", action="store_true", help="collecter 22 mois d'historique")
    ap.add_argument("--universe", type=int, default=60)
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--rank", action="store_true", help="classement du jour")
    ap.add_argument("--factor", default="", help="facteur utilise pour --rank")
    ap.add_argument("--horizon", type=int, default=180)
    args = ap.parse_args()

    key = os.environ.get("LUNARCRUSH_API_KEY")

    if args.fetch:
        coins = build_universe(args, key)
        print(f"Univers : {len(coins)} cryptos", file=sys.stderr)
        for n, c in enumerate(coins, 1):
            s = c["symbol"].upper()
            print(f"  [{n}/{len(coins)}] {s}", file=sys.stderr)
            try:
                load_long(s, key)
            except Exception as e:
                print(f"    echec : {e}", file=sys.stderr)
        (LCACHE / "_universe.json").write_text(json.dumps(coins))
        return

    data = panel()
    print(f"{len(data)} cryptos exploitables en cache", file=sys.stderr)

    if args.backtest:
        res, cov = run_backtest(data)
        for h in sorted(res):
            n_dates = max((len(v) for v in res[h].values()), default=0)
            print(f"\n{'='*72}\nIC moyen — horizon {h} jours ({n_dates} dates de test)\n{'='*72}")
            print(f"{'facteur':<22}{'IC moyen':>10}{'IC median':>11}{'% dates >0':>12}{'n':>5}")
            print("-" * 72)
            rank = []
            for fac in FACTORS:
                ics = res[h][fac]
                if len(ics) < 5:
                    continue
                m = statistics.mean(ics)
                rank.append((abs(m), m, fac, ics))
            for _, m, fac, ics in sorted(rank, reverse=True):
                pos = sum(1 for x in ics if x > 0) / len(ics) * 100
                print(f"{fac:<22}{m:>+10.3f}{statistics.median(ics):>+11.3f}{pos:>11.0f}%{len(ics):>5}")
        return

    if args.rank:
        fac = args.factor or "contrib_growth"
        print(f"\nClassement du jour — facteur {fac}")
        scored = []
        for s, rows in data.items():
            f = compute_factors(rows, len(rows) - 1)
            if f and f.get(fac) is not None:
                scored.append((f[fac], s, f, rows))
        for v, s, f, rows in sorted(scored, reverse=True)[:20]:
            print(f"  {s:<8}{v:>+9.2f}  dd12m {f['drawdown_12m']:>+6.2f}  "
                  f"mom6m {f['mom_6m'] or 0:>+6.2f}  attn {f['attention_ratio'] or 0:>6.2f}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
