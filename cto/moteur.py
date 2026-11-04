#!/usr/bin/env python3
"""Moteur du « Trader CTO » de l'app Mon Patrimoine : compte-titres FICTIF de 1 000 € chez Trade Republic (simulé).

Tout le calcul passe par ce script ; l'agent ne fait que choisir ses ordres.

Commandes
  init     --out DIR                       crée DIR/compte.json (1 000 € d'espèces, aucune ligne)
  pas      --compte FICHIER --out DIR      1) récupère les cours (Yahoo, barres de 5 min)
                                           2) exécute les stops / objectifs / stops suiveurs touchés depuis le passage précédent
                                           3) valorise le compte et scanne l'univers (momentum intraday)
                                           écrit DIR/pre.json et DIR/scan.json, affiche un résumé compact
  appliquer --decisions FICHIER --out DIR  exécute les ordres de l'agent au dernier cours (glissement 0,05 %, 1 € par ordre),
                                           ajoute le point de courbe et la note, écrit DIR/compte.json (document cto/compte)

Règles (mandat) : ordres au marché uniquement pendant la séance régulière du titre, nombre entier d'actions,
1 € de frais par ordre (Trade Republic), stop obligatoire à l'achat (entre 0,5 % et 15 % sous le prix),
4 lignes au plus, 70 % de la valeur du compte au plus par ligne, pas de vente à découvert ni d'effet de levier,
plus aucun achat dans la journée si le compte perd 5 % depuis le premier passage du jour.
"""
import argparse, json, math, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover
    PARIS = timezone(timedelta(hours=1))

UA = {"User-Agent": "Mozilla/5.0"}
FRAIS = 1.0            # € par ordre
GLISSEMENT = 0.0005    # 0,05 % contre nous à chaque exécution au marché
MAX_LIGNES = 4
MAX_POIDS = 0.70
COUPE_CIRCUIT = 0.05
FRAICHEUR = 25 * 60    # une barre de plus de 25 min = pas de cotation exploitable (Euronext est différé de 15 min)
DEPART = 1000.0
REF = ("^NDX", "Nasdaq-100")

UNIVERS = {
    # États-Unis (USD) — grosses capitalisations très liquides et valeurs à forte volatilité
    "NVDA": "Nvidia", "TSLA": "Tesla", "AMD": "AMD", "META": "Meta", "AMZN": "Amazon", "AAPL": "Apple",
    "MSFT": "Microsoft", "GOOGL": "Alphabet", "AVGO": "Broadcom", "NFLX": "Netflix", "MU": "Micron",
    "PLTR": "Palantir", "COIN": "Coinbase", "MSTR": "Strategy", "HOOD": "Robinhood", "SMCI": "Super Micro",
    "UBER": "Uber", "SHOP": "Shopify", "ARM": "Arm", "MRVL": "Marvell",
    # Europe (EUR)
    "ASML.AS": "ASML", "MC.PA": "LVMH", "AIR.PA": "Airbus", "SAF.PA": "Safran", "SAP.DE": "SAP",
    "STMPA.PA": "STMicro", "IFX.DE": "Infineon", "BNP.PA": "BNP Paribas", "TTE.PA": "TotalEnergies",
    "SU.PA": "Schneider", "STLAP.PA": "Stellantis", "SIE.DE": "Siemens", "NOKIA.HE": "Nokia",
}


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load(p, default=None):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except FileNotFoundError:
        return default
    return d["data"] if isinstance(d, dict) and "data" in d and "id" in d else d


def dump(obj, p):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def r2(x):
    return round(float(x) + 0.0, 2)


def r4(x):
    return round(float(x) + 0.0, 4)


def get(url, tries=3):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.load(r)
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 + 2 * k)


def chart(t, rng="5d"):
    """Barres de 5 min sur 5 jours + infos de séance. None si indisponible."""
    try:
        res = get("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(t) + "?range=" + rng + "&interval=5m")["chart"]["result"][0]
    except Exception:
        return None
    meta, q = res["meta"], res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        o, h, l, v = q["open"][i], q["high"][i], q["low"][i], q["volume"][i]
        bars.append({"ts": ts, "o": o if o is not None else c, "h": h if h is not None else c, "l": l if l is not None else c, "c": c, "v": v or 0})
    time.sleep(0.15)
    if not bars:
        return None
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    ouvert = bool(reg.get("start") and reg["start"] <= now <= reg["end"] + 60 and now - bars[-1]["ts"] <= FRAICHEUR)
    d_last = datetime.fromtimestamp(bars[-1]["ts"], PARIS).date()
    avant = [b for b in bars if datetime.fromtimestamp(b["ts"], PARIS).date() < d_last]
    prev = avant[-1]["c"] if avant else (meta.get("chartPreviousClose") if rng == "1d" else None)
    return {"t": t, "devise": meta.get("currency") or "EUR", "bars": bars, "last": bars[-1]["c"], "ts": bars[-1]["ts"],
            "prev": prev, "ouvert": ouvert,
            "seance": (reg.get("start"), reg.get("end")), "nom": meta.get("shortName") or meta.get("longName") or t}


def fx_usd(cache):
    """Nombre de dollars pour 1 euro (EURUSD)."""
    if "EURUSD" not in cache:
        c = chart("EURUSD=X", "1d")
        cache["EURUSD"] = c["last"] if c else None
    return cache["EURUSD"]


def to_eur(px, devise, fx):
    if devise == "EUR":
        return px
    if devise == "USD" and fx:
        return px / fx
    raise ValueError("devise non gérée : " + str(devise))


def valeur(compte):
    return r2(compte["cash"] + sum(p["qte"] * p.get("cours_eur", p["pru_eur"]) for p in compte["positions"]))


def hhmm(ts):
    return datetime.fromtimestamp(ts, PARIS).strftime("%H:%M")


# ---------------------------------------------------------------- init
def cmd_init(a):
    c = {"schema": 1, "courtier": "Trade Republic (simulé)", "depart": DEPART, "debut": iso(), "cash": DEPART,
         "positions": [], "frais_cumules": 0.0, "nb_ordres": 0, "realise": 0.0, "valeur": DEPART,
         "ref": {"ticker": REF[0], "nom": REF[1]}, "jour": None, "dernier_ts": int(time.time()),
         "courbe": [], "ordres": [], "notes": [], "maj": iso(), "statut": "ok", "titre": "Compte ouvert : 1 000 € en espèces"}
    r = chart(REF[0], "1d")
    if r:
        c["ref"]["depart"] = r["last"]
        c["ref"]["dernier"] = r["last"]
    c["courbe"].append({"t": c["debut"], "v": DEPART, "r": 100.0 if r else None})
    dump(c, os.path.join(a.out, "compte.json"))
    print("Compte initialisé :", DEPART, "€")


# ---------------------------------------------------------------- pas
def executer_sorties(p, ch, depuis, fx):
    """Parcourt les barres postérieures à `depuis` : stop suiveur, stop, objectif. Renvoie (prix, type, ts) ou None."""
    for b in ch["bars"]:
        if b["ts"] <= depuis:
            continue
        p["vu_ts"] = b["ts"]
        stop, obj = p.get("stop"), p.get("objectif")
        if stop is not None and b["o"] <= stop:
            return b["o"], "stop (ouverture sous le stop)", b["ts"]
        if stop is not None and b["l"] <= stop:
            return stop, "stop", b["ts"]
        if obj is not None and b["o"] >= obj:
            return b["o"], "objectif (ouverture au-dessus)", b["ts"]
        if obj is not None and b["h"] >= obj:
            return obj, "objectif", b["ts"]
        p["plus_haut"] = max(p.get("plus_haut") or b["h"], b["h"])
        if p.get("suiveur_pct"):
            ns = p["plus_haut"] * (1 - p["suiveur_pct"] / 100)
            if stop is None or ns > stop:
                p["stop"] = r4(ns)
    return None


def vendre(compte, p, prix_natif, fx, motif, kind, ts, out_ordres):
    prix_eur = to_eur(prix_natif, p["devise"], fx)
    brut = p["qte"] * prix_eur
    net = brut - FRAIS
    pnl = net - p["qte"] * p["pru_eur"] - p.get("frais_achat", FRAIS)
    compte["cash"] = r2(compte["cash"] + net)
    compte["frais_cumules"] = r2(compte["frais_cumules"] + FRAIS)
    compte["nb_ordres"] += 1
    compte["realise"] = r2(compte["realise"] + pnl)
    o = {"ts": iso(datetime.fromtimestamp(ts, timezone.utc)), "sens": "vente", "type": kind, "ticker": p["ticker"], "nom": p["nom"],
         "qte": p["qte"], "prix": r4(prix_natif), "devise": p["devise"], "montant_eur": r2(brut), "frais": FRAIS,
         "pnl_eur": r2(pnl), "pnl_pct": r2(100 * pnl / (p["qte"] * p["pru_eur"] + p.get("frais_achat", FRAIS))), "motif": motif}
    out_ordres.append(o)
    compte["positions"] = [x for x in compte["positions"] if x is not p]
    return o


def indicateurs(ch, fx):
    bars = ch["bars"]
    day0 = datetime.fromtimestamp(bars[-1]["ts"], PARIS).date()
    jour = [b for b in bars if datetime.fromtimestamp(b["ts"], PARIS).date() == day0]
    last = ch["last"]
    def chg(n):
        return 100 * (last / bars[-1 - n]["c"] - 1) if len(bars) > n else None
    closes = [b["c"] for b in bars[-15:]]
    gains = [max(0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    pertes = [max(0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, ap = (sum(gains) / len(gains), sum(pertes) / len(pertes)) if gains else (0, 0)
    rsi = 100 if ap == 0 else 100 - 100 / (1 + ag / ap)
    vol_j = [b["v"] for b in jour]
    vr = None
    if len(vol_j) >= 8 and sum(vol_j[:-6]) > 0:
        vr = (sum(vol_j[-6:]) / 6) / (sum(vol_j[:-6]) / len(vol_j[:-6]))
    hi, lo = max(b["h"] for b in jour), min(b["l"] for b in jour)
    # amplitude moyenne d'une barre de 5 min (en %), utile pour placer les stops
    rng = [100 * (b["h"] - b["l"]) / b["c"] for b in bars[-24:] if b["c"]]
    return {
        "ticker": ch["t"], "nom": UNIVERS.get(ch["t"], ch["nom"]), "devise": ch["devise"], "ouvert": ch["ouvert"],
        "ts": ch["ts"], "cours": r4(last), "cours_eur": r4(to_eur(last, ch["devise"], fx)), "heure": hhmm(ch["ts"]),
        "jour_pct": r2(100 * (last / ch["prev"] - 1)) if ch["prev"] else None,
        "depuis_ouv_pct": r2(100 * (last / jour[0]["o"] - 1)),
        "h1_pct": r2(chg(12)) if chg(12) is not None else None, "m15_pct": r2(chg(3)) if chg(3) is not None else None,
        "rsi14_5m": round(rsi), "vol_ratio": round(vr, 2) if vr else None,
        "pos_range": round((last - lo) / (hi - lo), 2) if hi > lo else None,
        "plus_haut_j": r4(hi), "plus_bas_j": r4(lo), "barre_moy_pct": r2(sum(rng) / len(rng)) if rng else None,
    }


def cmd_pas(a):
    compte = load(a.compte)
    if not compte:
        sys.exit("compte introuvable : " + a.compte)
    cache, ordres_auto = {}, []
    fx = fx_usd(cache)
    depuis = int(compte.get("dernier_ts") or 0)
    charts = {}
    tickers = list(UNIVERS) + [p["ticker"] for p in compte["positions"] if p["ticker"] not in UNIVERS]
    for t in tickers:
        ch = chart(t)
        if ch:
            charts[t] = ch
    if not charts:
        sys.exit("ERREUR : aucun cours récupéré (Yahoo injoignable ?)")
    if fx is None and any(c["devise"] == "USD" for c in charts.values()):
        sys.exit("ERREUR : taux EUR/USD indisponible")
    # 1) sorties automatiques
    for p in list(compte["positions"]):
        ch = charts.get(p["ticker"])
        if not ch:
            continue
        hit = executer_sorties(p, ch, p.get("vu_ts") or p.get("ouvert_ts", depuis), fx)
        if hit:
            vendre(compte, p, hit[0], fx, "Sortie automatique : " + hit[1], hit[1], hit[2], ordres_auto)
    # 2) valorisation
    stale = []
    for p in compte["positions"]:
        ch = charts.get(p["ticker"])
        if ch:
            p["cours"] = r4(ch["last"])
            p["cours_eur"] = r4(to_eur(ch["last"], p["devise"], fx))
            p["maj"] = iso(datetime.fromtimestamp(ch["ts"], timezone.utc))
            p["ouvert"] = ch["ouvert"]
        else:
            stale.append(p["ticker"])
    compte["valeur"] = valeur(compte)
    compte["dernier_ts"] = int(time.time())
    today = datetime.now(PARIS).date().isoformat()
    if not compte.get("jour") or compte["jour"].get("d") != today:
        compte["jour"] = {"d": today, "v0": compte["valeur"]}
    perte_j = compte["valeur"] / compte["jour"]["v0"] - 1
    compte["coupe_circuit"] = perte_j <= -COUPE_CIRCUIT
    ref = chart(compte["ref"]["ticker"], "1d")
    if ref:
        compte["ref"]["dernier"] = ref["last"]
        compte["ref"].setdefault("depart", ref["last"])
    compte["_ordres_auto"] = ordres_auto
    compte["_fx"] = fx
    scan = sorted((indicateurs(c, fx) for t, c in charts.items()), key=lambda x: (not x["ouvert"], -(x["h1_pct"] or -99)))
    dump(compte, os.path.join(a.out, "pre.json"))
    dump(scan, os.path.join(a.out, "scan.json"))
    # résumé compact pour l'agent
    eq = compte["valeur"]
    print("=== COMPTE ===")
    print("valeur %.2f € | espèces %.2f € | depuis le départ %+.2f %% | aujourd'hui %+.2f %% | frais cumulés %.2f € | ordres %d%s"
          % (eq, compte["cash"], 100 * (eq / compte["depart"] - 1), 100 * perte_j, compte["frais_cumules"], compte["nb_ordres"],
             " | COUPE-CIRCUIT ACTIF : aucun achat aujourd'hui" if compte["coupe_circuit"] else ""))
    print("EUR/USD %.4f | marchés ouverts : %s" % (fx or 0, ", ".join(sorted({("US" if c["devise"] == "USD" else "EU") for c in charts.values() if c["ouvert"]})) or "aucun"))
    for o in ordres_auto:
        print("EXÉCUTÉ AUTO : %s %d %s à %.4f %s → %+.2f € (%s)" % (o["sens"], o["qte"], o["ticker"], o["prix"], o["devise"], o["pnl_eur"], o["type"]))
    print("=== LIGNES ===")
    if not compte["positions"]:
        print("(aucune)")
    for p in compte["positions"]:
        pv = p["qte"] * (p.get("cours_eur", p["pru_eur"]) - p["pru_eur"])
        print("%s %s : %d × %.4f %s (PRU %.4f) = %.2f € | latent %+.2f € | stop %s | objectif %s | suiveur %s %% | %s"
              % (p["ticker"], p["nom"], p["qte"], p.get("cours", 0), p["devise"], p["pru"], p["qte"] * p.get("cours_eur", 0), pv,
                 p.get("stop"), p.get("objectif"), p.get("suiveur_pct"), "coté" if p.get("ouvert") else "FERMÉ"))
    if stale:
        print("SANS COURS :", ", ".join(stale))
    print("=== SCAN (ouverts d'abord, tri sur 1 h) ===")
    print("ticker | ouvert | cours dev | €  | jour% | ouv% | 1h% | 15m% | RSI | vol× | pos.range | barre moy%")
    for s in scan:
        print("%s | %s | %s %s | %s | %s | %s | %s | %s | %s | %s | %s | %s" % (
            s["ticker"], "O" if s["ouvert"] else "-", s["cours"], s["devise"], s["cours_eur"], s["jour_pct"], s["depuis_ouv_pct"],
            s["h1_pct"], s["m15_pct"], s["rsi14_5m"], s["vol_ratio"], s["pos_range"], s["barre_moy_pct"]))


# ---------------------------------------------------------------- appliquer
def cmd_appliquer(a):
    compte = load(os.path.join(a.out, "pre.json"))
    scan = {s["ticker"]: s for s in load(os.path.join(a.out, "scan.json"), [])}
    dec = load(a.decisions) or {}
    fx = compte.pop("_fx", None)
    ordres = compte.pop("_ordres_auto", [])
    rejets = []
    now = int(time.time())
    for o in dec.get("ordres") or []:
        act, t = o.get("action"), str(o.get("ticker") or "").strip()
        motif = str(o.get("motif") or "")[:400]
        s = scan.get(t)
        p = next((x for x in compte["positions"] if x["ticker"] == t), None)
        try:
            if act == "vente":
                if not p:
                    raise ValueError("pas de ligne " + t)
                if not s or not s["ouvert"]:
                    raise ValueError(t + " n'est pas coté en ce moment")
                vendre(compte, p, s["cours"] * (1 - GLISSEMENT), fx, motif, "marché", now, ordres)
            elif act == "ajuster":
                if not p:
                    raise ValueError("pas de ligne " + t)
                px = p.get("cours", p["pru"])
                if "stop" in o:
                    if o["stop"] is None or not (0 < float(o["stop"]) < px):
                        raise ValueError("stop invalide (doit rester sous le cours %.4f)" % px)
                    p["stop"] = r4(o["stop"])
                if "objectif" in o:
                    if o["objectif"] is not None and float(o["objectif"]) <= px:
                        raise ValueError("objectif sous le cours")
                    p["objectif"] = r4(o["objectif"]) if o["objectif"] is not None else None
                if "suiveur_pct" in o:
                    v = o["suiveur_pct"]
                    if v is not None and not (0.3 <= float(v) <= 15):
                        raise ValueError("suiveur entre 0,3 et 15 %")
                    p["suiveur_pct"] = float(v) if v is not None else None
                    p["plus_haut"] = max(p.get("plus_haut") or px, px)
                ordres.append({"ts": iso(), "sens": "ajustement", "type": "ajustement", "ticker": t, "nom": p["nom"], "stop": p.get("stop"),
                               "objectif": p.get("objectif"), "suiveur_pct": p.get("suiveur_pct"), "motif": motif})
            elif act == "achat":
                if compte.get("coupe_circuit"):
                    raise ValueError("coupe-circuit actif (−5 % sur la journée)")
                if not s:
                    raise ValueError(t + " absent du scan")
                if not s["ouvert"]:
                    raise ValueError(t + " n'est pas coté en ce moment")
                if p:
                    raise ValueError("déjà en portefeuille : renforcer est interdit, ajustez ou vendez")
                if len(compte["positions"]) >= MAX_LIGNES:
                    raise ValueError("%d lignes au maximum" % MAX_LIGNES)
                px = s["cours"] * (1 + GLISSEMENT)
                px_eur = to_eur(px, s["devise"], fx)
                montant = float(o.get("montant") or 0)
                eq = valeur(compte)
                montant = min(montant, compte["cash"] - FRAIS, MAX_POIDS * eq)
                qte = int(math.floor(montant / px_eur))
                if qte < 1:
                    raise ValueError("montant insuffisant pour une action à %.2f €" % px_eur)
                stop = o.get("stop")
                if stop is None or not (px * 0.85 <= float(stop) <= px * 0.995):
                    raise ValueError("stop obligatoire, entre 0,5 %% et 15 %% sous %.4f" % px)
                obj = o.get("objectif")
                if obj is not None and float(obj) <= px:
                    raise ValueError("objectif sous le prix")
                sv = o.get("suiveur_pct")
                if sv is not None and not (0.3 <= float(sv) <= 15):
                    raise ValueError("suiveur entre 0,3 et 15 %")
                cout = qte * px_eur
                compte["cash"] = r2(compte["cash"] - cout - FRAIS)
                compte["frais_cumules"] = r2(compte["frais_cumules"] + FRAIS)
                compte["nb_ordres"] += 1
                compte["positions"].append({"ticker": t, "nom": s["nom"], "devise": s["devise"], "qte": qte, "pru": r4(px), "pru_eur": r4(px_eur),
                                            "frais_achat": FRAIS, "stop": r4(stop), "objectif": r4(obj) if obj is not None else None,
                                            "suiveur_pct": float(sv) if sv is not None else None, "plus_haut": r4(px),
                                            "ouvert_le": iso(), "ouvert_ts": now, "vu_ts": s["ts"], "cours": r4(s["cours"]), "cours_eur": r4(s["cours_eur"]),
                                            "maj": iso(), "ouvert": True})
                ordres.append({"ts": iso(), "sens": "achat", "type": "marché", "ticker": t, "nom": s["nom"], "qte": qte, "prix": r4(px),
                               "devise": s["devise"], "montant_eur": r2(cout), "frais": FRAIS, "stop": r4(stop),
                               "objectif": r4(obj) if obj is not None else None, "motif": motif})
            else:
                raise ValueError("action inconnue : " + str(act))
        except (ValueError, TypeError) as e:
            rejets.append({"ordre": o, "raison": str(e)})
    compte["valeur"] = valeur(compte)
    ref = compte.get("ref") or {}
    rbase = 100 * ref["dernier"] / ref["depart"] if ref.get("depart") and ref.get("dernier") else None
    compte["courbe"] = (compte.get("courbe") or []) + [{"t": iso(), "v": compte["valeur"], "r": round(rbase, 3) if rbase else None}]
    compte["courbe"] = compte["courbe"][-4000:]
    compte["ordres"] = (ordres[::-1] + (compte.get("ordres") or []))[:200]
    titre = str(dec.get("titre") or "Passage sans commentaire")[:160]
    note = {"t": iso(), "titre": titre, "texte": str(dec.get("analyse") or "")[:1500], "valeur": compte["valeur"],
            "ordres": [("%s %s %s" % (x["sens"], x.get("qte", ""), x["ticker"])).replace("  ", " ") for x in ordres],
            "rejets": [r["raison"] for r in rejets]}
    compte["notes"] = ([note] + (compte.get("notes") or []))[:40]
    compte["titre"] = titre
    compte["statut"] = "ok"
    compte["maj"] = iso()
    dump(compte, os.path.join(a.out, "compte.json"))
    print("valeur %.2f € (%+.2f %% depuis le départ) | espèces %.2f € | lignes : %s" % (
        compte["valeur"], 100 * (compte["valeur"] / compte["depart"] - 1), compte["cash"],
        ", ".join("%d %s" % (p["qte"], p["ticker"]) for p in compte["positions"]) or "aucune"))
    for x in ordres:
        print("ORDRE :", x["sens"], x.get("qte", ""), x["ticker"], x.get("prix", ""), x.get("devise", ""), ("→ %+.2f €" % x["pnl_eur"]) if "pnl_eur" in x else "")
    for r in rejets:
        print("REJETÉ :", r["ordre"].get("action"), r["ordre"].get("ticker"), "—", r["raison"])


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("init"); x.add_argument("--out", required=True)
    x = sp.add_parser("pas"); x.add_argument("--compte", required=True); x.add_argument("--out", required=True)
    x = sp.add_parser("appliquer"); x.add_argument("--decisions", required=True); x.add_argument("--out", required=True)
    a = ap.parse_args()
    {"init": cmd_init, "pas": cmd_pas, "appliquer": cmd_appliquer}[a.cmd](a)


if __name__ == "__main__":
    main()
