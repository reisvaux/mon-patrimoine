#!/usr/bin/env python3
"""Onglet « Marchés » de l'app « Mon Patrimoine » : clôtures de la veille des grands indices boursiers.

Usage : python3 -I indices.txt --out <dossier> [--prev <marches/indices.json>]
(script Python publié en .txt dans l'app, comme cto/moteur.txt : l'hébergement ne sert pas les .py)
  --out  : écrit indices.json, le document marches/indices de l'app
  --prev : document actuel (fichier écrit par ArtifactData get/out_dir) ; sert seulement à garder la dernière
           clôture connue d'un indice que Yahoo ne renvoie pas aujourd'hui

Source : Yahoo Finance, historique quotidien (/v8/finance/chart, interval=1d).
« Veille » : pour chaque indice, la dernière séance TERMINÉE dont la date (dans le fuseau de sa place) est antérieure
à la date du jour à Paris. Une séance encore ouverte, ou close le jour même (Asie le matin), n'est jamais retenue.
Aucun chiffre n'est estimé : un indice sans cotation exploitable est listé dans « manquants ».
"""
import argparse, json, os, time, urllib.parse, urllib.request
from collections import Counter
from datetime import date, datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

UA = {"User-Agent": "Mozilla/5.0"}
MARGE_FIN = 20 * 60   # secondes après la clôture avant de considérer la séance du jour comme terminée
N_COURBE = 22         # points de la mini-courbe (environ un mois de bourse)
COURT = {"stoxx600": "Stoxx 600"}   # libellé du palmarès quand le nom est long

# id, symbole Yahoo, nom affiché, code pays (pastille), place, zone (eu / am / as / mo)
INDICES = [
    ("cac40", "^FCHI", "CAC 40", "FR", "Paris", "eu"),
    ("eurostoxx50", "^STOXX50E", "Euro Stoxx 50", "EU", "Zone euro", "eu"),
    ("stoxx600", "^STOXX", "Stoxx Europe 600", "EU", "Europe", "eu"),
    ("dax", "^GDAXI", "DAX", "DE", "Francfort", "eu"),
    ("ftse100", "^FTSE", "FTSE 100", "UK", "Londres", "eu"),
    ("ftsemib", "FTSEMIB.MI", "FTSE MIB", "IT", "Milan", "eu"),
    ("ibex35", "^IBEX", "IBEX 35", "ES", "Madrid", "eu"),
    ("smi", "^SSMI", "SMI", "CH", "Zurich", "eu"),
    ("sp500", "^GSPC", "S&P 500", "US", "New York", "am"),
    ("nasdaq", "^IXIC", "Nasdaq", "US", "Composite", "am"),
    ("nasdaq100", "^NDX", "Nasdaq-100", "US", "New York", "am"),
    ("dowjones", "^DJI", "Dow Jones", "US", "New York", "am"),
    ("russell2000", "^RUT", "Russell 2000", "US", "New York", "am"),
    ("tsx", "^GSPTSE", "S&P/TSX", "CA", "Toronto", "am"),
    ("bovespa", "^BVSP", "Bovespa", "BR", "São Paulo", "am"),
    ("nikkei", "^N225", "Nikkei 225", "JP", "Tokyo", "as"),
    ("hangseng", "^HSI", "Hang Seng", "HK", "Hong Kong", "as"),
    ("shanghai", "000001.SS", "Shanghai", "CN", "SSE Composite", "as"),
    ("kospi", "^KS11", "KOSPI", "KR", "Séoul", "as"),
    ("nifty50", "^NSEI", "Nifty 50", "IN", "Bombay", "as"),
    ("asx200", "^AXJO", "S&P/ASX 200", "AU", "Sydney", "as"),
    ("msciworld", "^990100-USD-STRD", "MSCI World", "MSCI", "Pays développés · USD", "mo"),
]


def paris_now():
    if ZoneInfo:
        return datetime.now(ZoneInfo("Europe/Paris"))
    return datetime.now(timezone(timedelta(hours=1)))


def get(url, tries=3):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.load(r)
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 + 2 * k)


def load(p):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (FileNotFoundError, ValueError, TypeError):
        return None
    return d["data"] if isinstance(d, dict) and "data" in d and "id" in d else d


def seances(sym, now_ts):
    """Séances terminées d'un indice : liste triée de (date ISO dans le fuseau de la place, clôture), et devise."""
    res = get("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) + "?range=2y&interval=1d")["chart"]["result"][0]
    meta = res["meta"]
    tz = None
    if ZoneInfo and meta.get("exchangeTimezoneName"):
        try:
            tz = ZoneInfo(meta["exchangeTimezoneName"])
        except Exception:
            tz = None
    if tz is None:
        tz = timezone(timedelta(seconds=int(meta.get("gmtoffset") or 0)))
    closes = res["indicators"]["quote"][0]["close"]
    by_day = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        c = closes[i]
        if c is None or not c > 0:
            continue
        by_day[datetime.fromtimestamp(ts, tz).date().isoformat()] = float(c)
    # la séance du jour (dans le fuseau de la place) ne compte que si elle est terminée
    local_today = datetime.fromtimestamp(now_ts, tz).date().isoformat()
    fin = ((meta.get("currentTradingPeriod") or {}).get("regular") or {}).get("end")
    if local_today in by_day and not (fin and now_ts >= fin + MARGE_FIN):
        del by_day[local_today]
    time.sleep(0.25)
    return sorted(by_day.items()), meta.get("currency")


def ratio(a, b):
    return round(a / b - 1, 6) if a and b else None


def moins_un_mois(d):
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    day = d.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev", default="")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    now = paris_now()
    today = now.date().isoformat()
    now_ts = time.time()
    prev = {x.get("id"): x for x in ((load(a.prev) or {}).get("indices") or []) if isinstance(x, dict)} if a.prev else {}

    out, manquants, repris = [], [], []
    for iid, sym, nom, pays, lieu, zone in INDICES:
        try:
            serie, devise = seances(sym, now_ts)
        except Exception:
            serie, devise = [], None
        serie = [(d, c) for d, c in serie if d < today]
        if len(serie) < 2:
            if iid in prev and prev[iid].get("d") and prev[iid].get("c"):
                out.append(prev[iid]); repris.append(nom)
            else:
                manquants.append(nom)
            continue
        d, c = serie[-1]
        p = serie[-2][1]
        dd = date.fromisoformat(d)
        avant = lambda lim: next((x[1] for x in reversed(serie) if x[0] <= lim), None)
        e = {"id": iid, "sym": sym, "nom": nom, "pays": pays, "lieu": lieu, "zone": zone,
             "d": d, "c": round(c, 2), "p": round(p, 2), "pd": serie[-2][0],
             "j": ratio(c, p),                                              # variation sur la séance de la veille
             "s": ratio(c, serie[-6][1]) if len(serie) >= 6 else None,      # 5 séances
             "m": ratio(c, avant(moins_un_mois(dd).isoformat())),           # 1 mois calendaire
             "a": ratio(c, avant(f"{dd.year - 1}-12-31")),                  # depuis le 1er janvier
             "h": [round(x[1], 2) for x in serie[-N_COURBE:]]}
        if devise:
            e["dev"] = devise
        if iid in COURT:
            e["court"] = COURT[iid]
        out.append(e)

    dates = Counter(x["d"] for x in out if x.get("d"))
    doc = {"majAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "jour": today,
           "veille": dates.most_common(1)[0][0] if dates else None,
           "source": "Yahoo Finance", "indices": out, "manquants": manquants}
    if not out:
        raise SystemExit("Aucun indice récupéré : rien n'est écrit.")
    json.dump(doc, open(os.path.join(a.out, "indices.json"), "w", encoding="utf-8"), ensure_ascii=False)
    hausse = sum(1 for x in out if (x.get("j") or 0) > 0)
    baisse = sum(1 for x in out if (x.get("j") or 0) < 0)
    print(json.dumps({"veille": doc["veille"], "indices": len(out), "hausse": hausse, "baisse": baisse,
                      "manquants": manquants, "repris_de_la_veille": repris,
                      "reperes": {x["nom"]: [x["c"], x["j"]] for x in out if x["id"] in ("cac40", "sp500", "nasdaq", "dowjones", "nikkei")}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
