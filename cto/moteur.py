#!/usr/bin/env python3
"""Moteur du « Trader CTO » de l'app Mon Patrimoine : compte-titres FICTIF de 1 000 € chez Trade Republic (simulé).
Version 2 (16/11/2026) : méthode day-trader (« The Trader skill v2 »).
Version 2.1 (16/11/2026) : glissement sur les stops et la mise à plat (la perte max en tient compte), nombres non finis
refusés, barre partielle après un achat au marché revue au passage suivant, verdicts de la Time Machine avec marge
d'erreur (pas de conclusion sous 15 cas ou quand le point mort reste dans la marge).

Tout le calcul passe par ce script ; l'agent ne fait que choisir ses ordres.

Commandes
  init      --out DIR                      crée DIR/compte.json (1 000 € d'espèces, aucune ligne)
  pas       --compte FICHIER --out DIR     1) récupère les cours (Yahoo, barres de 5 min)
                                           2) déclenche, barre par barre, les ordres « cassure » en attente
                                           3) exécute stops / objectifs / stops suiveurs et la mise à plat avant la clôture
                                           4) valorise le compte et scanne l'univers (momentum, range d'ouverture)
                                           écrit DIR/pre.json et DIR/scan.json, affiche un résumé compact
  appliquer --decisions FICHIER --out DIR  exécute les ordres de l'agent (glissement 0,05 %, 1 € par ordre),
                                           ajoute le point de courbe et la note, écrit DIR/compte.json (document cto/compte)
  tm        --ticker T [--entree P] --stop S --objectif O
                                           Time Machine : 10 000 rejeux du reste de la séance (graine 19)
  tm        --ticker T --orb [--r 2] [--regle-stop or-mid|or-low]
                                           historique de la règle « cassure du range d'ouverture » sur ~60 jours

Règles du moteur (il refuse tout ordre hors règles)
  ordres au marché pendant la séance régulière du titre, nombre entier d'actions, 1 € de frais par ordre,
  4 lignes au plus, 70 % du compte au plus par ligne, pas de renforcement, pas de vente à découvert ni de levier,
  coupe-circuit : plus aucun achat dans la journée si le compte perd 5 % depuis le premier passage du jour.
  Day trading (v2) : stop obligatoire (0,5 à 15 % sous le prix) ET objectif obligatoire (gain:risque ≥ 1),
  perte au stop ≤ 10 € par trade frais d'aller-retour compris, ≤ 30 € pour l'ensemble des entrées du jour,
  3 entrées et 10 ordres par jour au plus, 10 % du compte gardés en espèces, ordre de 5 à 700 €,
  entrées seulement de l'ouverture + 15 min à la clôture − 30 min, tout est vendu 5 min avant la clôture.
"""
import argparse, json, math, os, random, sys, time, urllib.parse, urllib.request
from collections import Counter
from datetime import date, datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    PARIS = timezone(timedelta(hours=1))
    NY = timezone(timedelta(hours=-5))

UA = {"User-Agent": "Mozilla/5.0"}
FRAIS = 1.0            # € par ordre
GLISSEMENT = 0.0005    # 0,05 % contre nous à chaque exécution au marché
MAX_LIGNES = 4
MAX_POIDS = 0.70
COUPE_CIRCUIT = 0.05
FRAICHEUR = 25 * 60    # une barre de plus de 25 min = pas de cotation exploitable (Euronext est différé de 15 min)
DEPART = 1000.0
REF = ("^NDX", "Nasdaq-100")
BARRE = 300            # barres de 5 min

# --- limites day-trader (validées par Aurelian le 16/11/2026 pour 10 € / 30 €, le reste par défaut)
PERTE_MAX_TRADE = 10.0   # € perdus au plus si le stop est touché, frais d'aller-retour compris
PERTE_MAX_JOUR = 30.0    # somme des pertes max des entrées du jour
MAX_TRADES_JOUR = 3
MAX_ORDRES_JOUR = 10
GAIN_RISQUE_MIN = 1.0
ORDRE_MIN = 5.0
ORDRE_MAX = 700.0
RESERVE = 0.10
OR_MIN = 15              # durée du range d'ouverture (min)
ENTREE_APRES = 15        # pas d'entrée avant l'ouverture + 15 min
ENTREE_AVANT = 30        # pas d'entrée dans les 30 dernières minutes
A_PLAT = 5               # tout est vendu 5 min avant la clôture

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


def iso_ts(ts):
    return iso(datetime.fromtimestamp(ts, timezone.utc))


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


def bas_centime(x):
    """Arrondi au centime vers le bas (un stop s'éloigne de l'entrée)."""
    return math.floor(float(x) * 100 + 1e-9) / 100


def haut_centime(x):
    """Arrondi au centime vers le haut (objectif tiré d'un multiple de R : le gain:risque ne passe jamais sous R)."""
    return math.ceil(float(x) * 100 - 1e-6) / 100


def nombre(x, quoi):
    """Nombre fini, sinon ValueError : refuse NaN, l'infini, le texte et les booléens."""
    if isinstance(x, bool):
        raise ValueError("%s : nombre attendu, reçu %r" % (quoi, x))
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError("%s : nombre attendu, reçu %r" % (quoi, x))
    if not math.isfinite(v):
        raise ValueError("%s : nombre fini attendu, reçu %r" % (quoi, x))
    return v


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
    """Barres de 5 min + infos de séance. None si indisponible."""
    try:
        res = get("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(t) + "?range=" + rng + "&interval=5m")["chart"]["result"][0]
    except Exception:
        return None
    return chart_depuis(res, t, rng)


def chart_depuis(res, t, rng="5d"):
    meta, q = res["meta"], res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None or ts % BARRE:
            continue  # cours nul ou « tick » en direct hors grille de 5 min
        o, h, l, v = q["open"][i], q["high"][i], q["low"][i], q["volume"][i]
        bars.append({"ts": ts, "o": o if o is not None else c, "h": h if h is not None else c, "l": l if l is not None else c, "c": c, "v": v or 0})
    time.sleep(0.15)
    if not bars:
        return None
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    ouvert = bool(reg.get("start") and reg["start"] <= now <= reg["end"] + 60 and now - bars[-1]["ts"] <= FRAICHEUR)
    d_last = jour_paris(bars[-1]["ts"])
    avant = [b for b in bars if jour_paris(b["ts"]) < d_last]
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


def jour_paris(ts):
    return datetime.fromtimestamp(ts, PARIS).date()


def aujourdhui():
    return datetime.now(PARIS).date()


# ---------------------------------------------------------------- séances, range d'ouverture
def nominal(devise, d):
    """Horaires réguliers par défaut : NYSE/Nasdaq 9 h 30 – 16 h (New York), Europe 9 h – 17 h 30 (Paris)."""
    if devise == "USD":
        s = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)
        e = datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY)
    else:
        s = datetime(d.year, d.month, d.day, 9, 0, tzinfo=PARIS)
        e = datetime(d.year, d.month, d.day, 17, 30, tzinfo=PARIS)
    return int(s.timestamp()), int(e.timestamp())


def bornes(ch, d):
    """(début, fin) de la séance régulière du jour d (date de Paris) pour ce titre."""
    s, e = ch.get("seance") or (None, None)
    if s and e and jour_paris(s) == d:
        return int(s), int(e)
    if d < aujourdhui():
        bj = [b["ts"] for b in ch["bars"] if jour_paris(b["ts"]) == d]
        if bj:
            return bj[0], bj[-1] + BARRE
    return nominal(ch["devise"], d)


def fenetre(ch, d):
    s, e = bornes(ch, d)
    return {"debut": s, "fin": e, "entree_de": s + ENTREE_APRES * 60, "entree_a": e - ENTREE_AVANT * 60, "a_plat": e - A_PLAT * 60}


def range_ouverture(ch, d, now=None):
    """Range des OR_MIN premières minutes et premier déclenchement (barre terminée qui CLÔTURE au-dessus du haut)."""
    now = now or time.time()
    f = fenetre(ch, d)
    s = f["debut"]
    res = {"debut": s, "fin": f["fin"]}
    if now < s:
        res["statut"] = "pas ouvert"
        return res
    bars = [b for b in ch["bars"] if s <= b["ts"] < f["fin"] and jour_paris(b["ts"]) == d]
    rng = [b for b in bars if b["ts"] < s + OR_MIN * 60]
    n = OR_MIN * 60 // BARRE
    if len(rng) < n or rng[-1]["ts"] + BARRE > now:
        res["statut"] = "en formation" if now < s + OR_MIN * 60 + 20 * 60 else "données incomplètes"
        return res
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    res.update(haut=r4(hi), bas=r4(lo), mid=r4((hi + lo) / 2))
    for b in bars:
        if b["ts"] < s + OR_MIN * 60 or b["ts"] + BARRE > now:
            continue
        if b["ts"] + BARRE > f["entree_a"]:
            break
        if b["c"] > hi:
            res.update(statut="déclenché", dec_ts=b["ts"], dec_px=r4(b["c"]))
            return res
    res["statut"] = "expiré" if now >= f["entree_a"] else "en attente"
    return res


# ---------------------------------------------------------------- init
def cmd_init(a):
    c = {"schema": 2, "courtier": "Trade Republic (simulé)", "depart": DEPART, "debut": iso(), "cash": DEPART,
         "positions": [], "attente": [], "frais_cumules": 0.0, "nb_ordres": 0, "realise": 0.0, "valeur": DEPART,
         "ref": {"ticker": REF[0], "nom": REF[1]}, "jour": None, "dernier_ts": int(time.time()), "daytrade_depuis": int(time.time()),
         "courbe": [], "ordres": [], "notes": [], "maj": iso(), "statut": "ok", "titre": "Compte ouvert : 1 000 € en espèces"}
    r = chart(REF[0], "1d")
    if r:
        c["ref"]["depart"] = r["last"]
        c["ref"]["dernier"] = r["last"]
    c["courbe"].append({"t": c["debut"], "v": DEPART, "r": 100.0 if r else None})
    dump(c, os.path.join(a.out, "compte.json"))
    print("Compte initialisé :", DEPART, "€")


# ---------------------------------------------------------------- journée
def jour_courant(compte, v):
    today = aujourdhui().isoformat()
    j = compte.get("jour") or {}
    if j.get("d") != today:
        j = {"d": today, "v0": v}
    for k, z in (("entrees", 0), ("ordres", 0), ("risque", 0.0)):
        j.setdefault(k, z)
    compte["jour"] = j
    return j


# ---------------------------------------------------------------- ouverture d'une ligne (Risk Manager + Budget Manager)
def ouvrir(compte, t, nom, devise, px, stop, obj, sv, montant_max, fx, ts, motif, kind, ordres, extra=None):
    """Contrôle l'entrée, calcule la taille et ouvre la ligne. Lève ValueError avec la raison."""
    j = compte["jour"]
    if compte.get("coupe_circuit"):
        raise ValueError("coupe-circuit actif (−5 % sur la journée)")
    if any(p["ticker"] == t for p in compte["positions"]):
        raise ValueError("déjà en portefeuille : renforcer est interdit, ajustez ou vendez")
    if len(compte["positions"]) >= MAX_LIGNES:
        raise ValueError("%d lignes au maximum" % MAX_LIGNES)
    if j["entrees"] >= MAX_TRADES_JOUR:
        raise ValueError("%d entrées par jour au maximum" % MAX_TRADES_JOUR)
    if j["ordres"] >= MAX_ORDRES_JOUR:
        raise ValueError("%d ordres par jour au maximum" % MAX_ORDRES_JOUR)
    px = nombre(px, "prix")
    if px <= 0:
        raise ValueError("prix invalide : %r" % px)
    if stop is None:
        raise ValueError("stop obligatoire, entre 0,5 %% et 15 %% sous %.4f" % px)
    stop = nombre(stop, "stop")
    if not (px * 0.85 <= stop <= px * 0.995):
        raise ValueError("stop obligatoire, entre 0,5 %% et 15 %% sous %.4f" % px)
    if obj is None:
        raise ValueError("objectif obligatoire (gain:risque ≥ %.1f)" % GAIN_RISQUE_MIN)
    obj = nombre(obj, "objectif")
    gr = (obj - px) / (px - stop)
    if gr < GAIN_RISQUE_MIN - 1e-6:
        raise ValueError("gain:risque %.2f sous le minimum %.1f (objectif trop proche)" % (gr, GAIN_RISQUE_MIN))
    if sv is not None:
        sv = nombre(sv, "suiveur")
        if not (0.3 <= sv <= 15):
            raise ValueError("suiveur entre 0,3 et 15 %")
    if montant_max:
        montant_max = nombre(montant_max, "montant")
        if montant_max <= 0:
            raise ValueError("montant invalide : %r" % montant_max)
    px_eur = to_eur(px, devise, fx)
    # le stop est un ordre au marché une fois touché : il subit le glissement, la perte max en tient compte
    risque_eur = to_eur(px - stop * (1 - GLISSEMENT), devise, fx)
    eq = valeur(compte)
    reste_jour = PERTE_MAX_JOUR - j["risque"]
    if reste_jour - 2 * FRAIS <= 0:
        raise ValueError("perte max du jour déjà engagée (%.2f / %.0f €)" % (j["risque"], PERTE_MAX_JOUR))
    limites = {
        "perte max par trade (%.0f €)" % PERTE_MAX_TRADE: (PERTE_MAX_TRADE - 2 * FRAIS) / risque_eur,
        "perte max du jour (reste %.2f €)" % reste_jour: (reste_jour - 2 * FRAIS) / risque_eur,
        "position max %d %%" % round(100 * MAX_POIDS): MAX_POIDS * eq / px_eur,
        "ordre max %.0f €" % ORDRE_MAX: ORDRE_MAX / px_eur,
        "espèces hors réserve de %d %%" % round(100 * RESERVE): (compte["cash"] - FRAIS - RESERVE * eq) / px_eur,
    }
    if montant_max:
        limites["montant demandé %.0f €" % float(montant_max)] = float(montant_max) / px_eur
    cle = min(limites, key=limites.get)
    qte = int(math.floor(limites[cle] + 1e-9))
    if qte < 1:
        raise ValueError("0 action possible (limite : %s ; risque %.2f € par action)" % (cle, risque_eur))
    cout = qte * px_eur
    if cout < ORDRE_MIN:
        raise ValueError("ordre de %.2f € sous le minimum de %.0f €" % (cout, ORDRE_MIN))
    perte = r2(qte * risque_eur + 2 * FRAIS)
    compte["cash"] = r2(compte["cash"] - cout - FRAIS)
    compte["frais_cumules"] = r2(compte["frais_cumules"] + FRAIS)
    compte["nb_ordres"] += 1
    j["entrees"] += 1
    j["ordres"] += 1
    j["risque"] = r2(j["risque"] + perte)
    pos = {"ticker": t, "nom": nom, "devise": devise, "qte": qte, "pru": r4(px), "pru_eur": r4(px_eur),
           "frais_achat": FRAIS, "stop": r4(stop), "objectif": r4(obj), "suiveur_pct": float(sv) if sv is not None else None,
           "plus_haut": r4(px), "ouvert_le": iso_ts(ts), "ouvert_ts": int(ts), "vu_ts": int(ts), "cours": r4(px), "cours_eur": r4(px_eur),
           "maj": iso_ts(ts), "ouvert": True, "perte_max_eur": perte, "gain_risque": r2(gr), "mode": kind}
    if extra:
        pos.update(extra)
    compte["positions"].append(pos)
    ordres.append({"ts": iso_ts(ts), "sens": "achat", "type": kind, "ticker": t, "nom": nom, "qte": qte, "prix": r4(px),
                   "devise": devise, "montant_eur": r2(cout), "frais": FRAIS, "stop": r4(stop), "objectif": r4(obj),
                   "perte_max_eur": perte, "gain_risque": r2(gr), "limite": cle, "motif": motif})
    return pos, cle


# ---------------------------------------------------------------- pas
def executer_sorties(p, ch, depuis, now, dt_depuis):
    """Parcourt les barres postérieures à `depuis` : stop, objectif, stop suiveur, puis mise à plat avant la clôture.
    Renvoie (prix, type, ts) ou None. Une barre encore en cours peut toucher un stop ou un objectif ;
    elle n'est marquée « vue » qu'une fois terminée.
    Stop et mise à plat sont des ventes au marché : glissement de 0,05 % contre nous. L'objectif est un ordre
    limité : il passe à son prix (ou à l'ouverture si elle est plus haute), sans glissement.
    Barre partielle (achat au marché en cours de barre) : seule la partie postérieure à l'achat compte."""
    j0 = jour_paris(p.get("ouvert_ts") or depuis or now)
    bp = p.get("barre_partielle") or {}
    for b in ch["bars"]:
        if b["ts"] <= depuis:
            continue
        fini = b["ts"] + BARRE <= now
        dj = jour_paris(b["ts"])
        if b["ts"] >= dt_depuis and dj > j0:
            return b["o"] * (1 - GLISSEMENT), "à plat (ligne restée ouverte la veille)", b["ts"]
        if bp and b["ts"] == bp.get("ts"):
            # avant l'achat, la barre avait déjà fait bp["l"]–bp["h"] : un nouveau plus bas (ou plus haut) est
            # forcément postérieur à l'achat ; sinon, l'après-achat reste entre le prix d'achat et la clôture
            px0 = bp["px"]
            b = {"ts": b["ts"], "o": px0, "c": b["c"],
                 "l": b["l"] if b["l"] < bp["l"] else min(px0, b["c"]),
                 "h": b["h"] if b["h"] > bp["h"] else max(px0, b["c"])}
        stop, obj = p.get("stop"), p.get("objectif")
        if stop is not None and b["o"] <= stop:
            return b["o"] * (1 - GLISSEMENT), "stop (ouverture sous le stop)", b["ts"]
        if stop is not None and b["l"] <= stop:
            return stop * (1 - GLISSEMENT), "stop", b["ts"]
        if obj is not None and b["o"] >= obj:
            return b["o"], "objectif (ouverture au-dessus)", b["ts"]
        if obj is not None and b["h"] >= obj:
            return obj, "objectif", b["ts"]
        p["plus_haut"] = max(p.get("plus_haut") or b["h"], b["h"])
        if p.get("suiveur_pct"):
            ns = p["plus_haut"] * (1 - p["suiveur_pct"] / 100)
            if stop is None or ns > stop:
                p["stop"] = r4(ns)
        if not fini:
            break
        p["vu_ts"] = b["ts"]
        if bp and b["ts"] >= bp.get("ts", 0):
            p.pop("barre_partielle", None)
            bp = {}
        if b["ts"] >= dt_depuis and b["ts"] + BARRE >= fenetre(ch, dj)["a_plat"]:
            return b["c"] * (1 - GLISSEMENT), "à plat avant la clôture", b["ts"] + BARRE
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
    o = {"ts": iso_ts(ts), "sens": "vente", "type": kind, "ticker": p["ticker"], "nom": p["nom"],
         "qte": p["qte"], "prix": r4(prix_natif), "devise": p["devise"], "montant_eur": r2(brut), "frais": FRAIS,
         "pnl_eur": r2(pnl), "pnl_pct": r2(100 * pnl / (p["qte"] * p["pru_eur"] + p.get("frais_achat", FRAIS))), "motif": motif}
    out_ordres.append(o)
    compte["positions"] = [x for x in compte["positions"] if x is not p]
    return o


def declencher_attente(compte, charts, fx, now, ordres_auto, evenements):
    """Ordres « cassure » : la première barre terminée (après le dépôt de l'ordre) qui CLÔTURE au-dessus du niveau
    déclenche l'achat au prix de clôture de cette barre + glissement, avec les contrôles du moteur à ce moment-là."""
    restants, declenches = [], []
    for a in compte.get("attente") or []:
        ch = charts.get(a["ticker"])
        d = date.fromisoformat(a["jour"])
        if not ch:
            if aujourdhui() > d:
                evenements.append("cassure %s expirée sans déclenchement" % a["ticker"])
            else:
                restants.append(a)
            continue
        f = fenetre(ch, d)
        dec = None
        if a["niveau"] == "orb":
            ro = range_ouverture(ch, d, now)
            if ro.get("statut") == "déclenché" and ro["dec_ts"] + BARRE > a["cree_ts"]:
                stop = a["stop"]
                if stop == "or-mid":
                    stop = bas_centime(ro["mid"])
                elif stop == "or-low":
                    stop = bas_centime(ro["bas"])
                dec = (ro["dec_ts"], ro["dec_px"], float(stop), "range %.2f–%.2f" % (ro["bas"], ro["haut"]), ro["haut"])
            elif ro.get("statut") == "déclenché":
                evenements.append("cassure %s annulée : le range avait cassé avant l'ordre" % a["ticker"])
                continue
        else:
            niv = float(a["niveau"])
            for b in ch["bars"]:
                if jour_paris(b["ts"]) != d or b["ts"] + BARRE <= a["cree_ts"] or b["ts"] + BARRE > now:
                    continue
                if b["ts"] + BARRE < f["entree_de"]:
                    continue
                if b["ts"] + BARRE > f["entree_a"]:
                    break
                if b["c"] > niv:
                    dec = (b["ts"], b["c"], float(a["stop"]), "niveau %.2f" % niv, niv)
                    break
        if dec:
            declenches.append((dec, a, ch))
        elif now >= f["entree_a"] or aujourdhui() > d:
            evenements.append("cassure %s expirée sans déclenchement" % a["ticker"])
        else:
            restants.append(a)
    for (bts, bpx, stop, quoi, niv), a, ch in sorted(declenches, key=lambda x: x[0][0]):
        px = bpx * (1 + GLISSEMENT)
        obj = a.get("objectif")
        if obj is None and a.get("r"):
            obj = haut_centime(px + float(a["r"]) * (px - stop))
        motif = "%s — barre de %s clôturée à %.2f au-dessus du %s" % (a.get("motif") or "Cassure", hhmm(bts), bpx, quoi)
        try:
            ouvrir(compte, a["ticker"], a["nom"], ch["devise"], px, stop, obj, a.get("suiveur_pct"), a.get("montant"), fx,
                   bts + BARRE, motif[:400], "cassure", ordres_auto, extra={"vu_ts": bts})
        except (ValueError, TypeError) as e:
            evenements.append("cassure %s refusée au déclenchement (%s) : %s" % (a["ticker"], hhmm(bts + BARRE), e))
    compte["attente"] = restants


def cmd_pas(a):
    compte = load(a.compte)
    if not compte:
        sys.exit("compte introuvable : " + a.compte)
    cache, ordres_auto, evenements = {}, [], []
    fx = fx_usd(cache)
    now = int(time.time())
    depuis = int(compte.get("dernier_ts") or 0)
    compte.setdefault("attente", [])
    compte.setdefault("daytrade_depuis", now)
    compte["schema"] = 2
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
    jour_courant(compte, valeur(compte))
    # 1) ordres en attente
    declencher_attente(compte, charts, fx, now, ordres_auto, evenements)
    # 2) sorties automatiques (stop, objectif, suiveur, à plat avant la clôture)
    for p in list(compte["positions"]):
        ch = charts.get(p["ticker"])
        if not ch:
            continue
        hit = executer_sorties(p, ch, p.get("vu_ts") or p.get("ouvert_ts", depuis), now, compte["daytrade_depuis"])
        if hit:
            vendre(compte, p, hit[0], fx, "Sortie automatique : " + hit[1], hit[1], hit[2], ordres_auto)
    # 3) valorisation
    stale = []
    for p in compte["positions"]:
        ch = charts.get(p["ticker"])
        if ch:
            p["cours"] = r4(ch["last"])
            p["cours_eur"] = r4(to_eur(ch["last"], p["devise"], fx))
            p["maj"] = iso_ts(ch["ts"])
            p["ouvert"] = ch["ouvert"]
        else:
            stale.append(p["ticker"])
    compte["valeur"] = valeur(compte)
    compte["dernier_ts"] = now
    j = jour_courant(compte, compte["valeur"])
    perte_j = compte["valeur"] / j["v0"] - 1
    compte["coupe_circuit"] = perte_j <= -COUPE_CIRCUIT
    ref = chart(compte["ref"]["ticker"], "1d")
    if ref:
        compte["ref"]["dernier"] = ref["last"]
        compte["ref"].setdefault("depart", ref["last"])
    compte["_ordres_auto"] = ordres_auto
    compte["_evenements"] = evenements
    compte["_fx"] = fx
    scan = sorted((indicateurs(c, fx, now) for t, c in charts.items()), key=lambda x: (not x["ouvert"], -(x["h1_pct"] or -99)))
    dump(compte, os.path.join(a.out, "pre.json"))
    dump(scan, os.path.join(a.out, "scan.json"))
    # résumé compact pour l'agent
    eq = compte["valeur"]
    print("=== COMPTE ===")
    print("valeur %.2f € | espèces %.2f € | depuis le départ %+.2f %% | aujourd'hui %+.2f %% | frais cumulés %.2f € | ordres %d%s"
          % (eq, compte["cash"], 100 * (eq / compte["depart"] - 1), 100 * perte_j, compte["frais_cumules"], compte["nb_ordres"],
             " | COUPE-CIRCUIT ACTIF : aucun achat aujourd'hui" if compte["coupe_circuit"] else ""))
    print("EUR/USD %.4f | marchés ouverts : %s" % (fx or 0, ", ".join(sorted({("US" if c["devise"] == "USD" else "EU") for c in charts.values() if c["ouvert"]})) or "aucun"))
    print("=== JOURNÉE (limites day-trader) ===")
    print("entrées %d/%d | ordres %d/%d | perte max engagée %.2f/%.0f € | perte max par trade %.0f € frais compris | gain:risque ≥ %.1f"
          % (j["entrees"], MAX_TRADES_JOUR, j["ordres"], MAX_ORDRES_JOUR, j["risque"], PERTE_MAX_JOUR, PERTE_MAX_TRADE, GAIN_RISQUE_MIN))
    today = aujourdhui()
    for dev, lab in (("EUR", "EU"), ("USD", "US")):
        c = next((c for c in charts.values() if c["devise"] == dev), None)
        if c:
            f = fenetre(c, today)
            print("%s : séance %s–%s | entrées %s–%s | à plat %s (heure de Paris)" % (
                lab, hhmm(f["debut"]), hhmm(f["fin"]), hhmm(f["entree_de"]), hhmm(f["entree_a"]), hhmm(f["a_plat"])))
    for o in ordres_auto:
        if o["sens"] == "achat":
            print("EXÉCUTÉ AUTO : achat %d %s à %.4f %s (cassure) | stop %s | objectif %s | perte max %.2f € | taille : %s"
                  % (o["qte"], o["ticker"], o["prix"], o["devise"], o["stop"], o["objectif"], o["perte_max_eur"], o["limite"]))
        else:
            print("EXÉCUTÉ AUTO : %s %d %s à %.4f %s → %+.2f € (%s)" % (o["sens"], o["qte"], o["ticker"], o["prix"], o["devise"], o["pnl_eur"], o["type"]))
    for e in evenements:
        print("INFO :", e)
    print("=== LIGNES ===")
    if not compte["positions"]:
        print("(aucune)")
    for p in compte["positions"]:
        pv = p["qte"] * (p.get("cours_eur", p["pru_eur"]) - p["pru_eur"])
        print("%s %s : %d × %.4f %s (PRU %.4f) = %.2f € | latent %+.2f € | stop %s | objectif %s | suiveur %s %% | perte max %s € | %s"
              % (p["ticker"], p["nom"], p["qte"], p.get("cours", 0), p["devise"], p["pru"], p["qte"] * p.get("cours_eur", 0), pv,
                 p.get("stop"), p.get("objectif"), p.get("suiveur_pct"), p.get("perte_max_eur", "?"), "coté" if p.get("ouvert") else "FERMÉ"))
    if stale:
        print("SANS COURS :", ", ".join(stale))
    print("=== ORDRES CASSURE EN ATTENTE ===")
    if not compte["attente"]:
        print("(aucun)")
    for x in compte["attente"]:
        print("%s : niveau %s | stop %s | %s | montant max %s | déposé à %s" % (
            x["ticker"], "range d'ouverture" if x["niveau"] == "orb" else x["niveau"], x["stop"],
            ("objectif %s" % x["objectif"]) if x.get("objectif") is not None else "objectif %sR" % x.get("r"),
            x.get("montant") or "—", hhmm(x["cree_ts"])))
    print("=== SCAN (ouverts d'abord, tri sur 1 h) ===")
    print("ticker | ouvert | cours dev | €  | jour% | ouv% | 1h% | 15m% | RSI | vol× | pos.range | barre moy% | range d'ouverture")
    for s in scan:
        print("%s | %s | %s %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s" % (
            s["ticker"], "O" if s["ouvert"] else "-", s["cours"], s["devise"], s["cours_eur"], s["jour_pct"], s["depuis_ouv_pct"],
            s["h1_pct"], s["m15_pct"], s["rsi14_5m"], s["vol_ratio"], s["pos_range"], s["barre_moy_pct"], texte_orb(s.get("orb"), s["cours"])))


def texte_orb(o, cours):
    if not o:
        return "—"
    st = o.get("statut")
    if "haut" not in o:
        return st
    base = "%s–%s" % (o["bas"], o["haut"])
    if st == "déclenché":
        return "%s déclenché %s à %s" % (base, hhmm(o["dec_ts"] + BARRE), o["dec_px"])
    if st == "en attente":
        return "%s attente (%+.2f %% du haut)" % (base, 100 * (cours / o["haut"] - 1))
    return "%s %s" % (base, st)


def indicateurs(ch, fx, now=None):
    bars = ch["bars"]
    day0 = jour_paris(bars[-1]["ts"])
    jour = [b for b in bars if jour_paris(b["ts"]) == day0]
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
    today = aujourdhui()
    f = fenetre(ch, today)
    return {
        "ticker": ch["t"], "nom": UNIVERS.get(ch["t"], ch["nom"]), "devise": ch["devise"], "ouvert": ch["ouvert"],
        "ts": ch["ts"], "cours": r4(last), "cours_eur": r4(to_eur(last, ch["devise"], fx)), "heure": hhmm(ch["ts"]),
        "jour_pct": r2(100 * (last / ch["prev"] - 1)) if ch["prev"] else None,
        "depuis_ouv_pct": r2(100 * (last / jour[0]["o"] - 1)),
        "h1_pct": r2(chg(12)) if chg(12) is not None else None, "m15_pct": r2(chg(3)) if chg(3) is not None else None,
        "rsi14_5m": round(rsi), "vol_ratio": round(vr, 2) if vr else None,
        "pos_range": round((last - lo) / (hi - lo), 2) if hi > lo else None,
        "plus_haut_j": r4(hi), "plus_bas_j": r4(lo), "barre_moy_pct": r2(sum(rng) / len(rng)) if rng else None,
        "orb": range_ouverture(ch, today, now), "fenetre": f,
        # dernière barre, pour qu'un achat au marché en cours de barre ne compte que la suite de cette barre
        "barre": {"ts": bars[-1]["ts"], "h": bars[-1]["h"], "l": bars[-1]["l"], "fini": bars[-1]["ts"] + BARRE <= (now or time.time())},
    }


# ---------------------------------------------------------------- appliquer
def cmd_appliquer(a):
    compte = load(os.path.join(a.out, "pre.json"))
    scan = {s["ticker"]: s for s in load(os.path.join(a.out, "scan.json"), [])}
    dec = load(a.decisions) or {}
    fx = compte.pop("_fx", None)
    ordres = compte.pop("_ordres_auto", [])
    evenements = compte.pop("_evenements", [])
    compte.setdefault("attente", [])
    j = jour_courant(compte, compte.get("valeur") or valeur(compte))
    rejets, deposes = [], []
    now = int(time.time())
    today = aujourdhui()
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
                j["ordres"] += 1
            elif act == "ajuster":
                if not p:
                    raise ValueError("pas de ligne " + t)
                px = p.get("cours", p["pru"])
                if "stop" in o:
                    if o["stop"] is None:
                        raise ValueError("stop invalide (doit rester sous le cours %.4f)" % px)
                    ns = nombre(o["stop"], "stop")
                    if not (0 < ns < px):
                        raise ValueError("stop invalide (doit rester sous le cours %.4f)" % px)
                    if ns < float(p.get("stop") or 0):
                        raise ValueError("un stop ne se baisse jamais (actuel %s)" % p.get("stop"))
                if "objectif" in o:
                    if o["objectif"] is None:
                        raise ValueError("l'objectif reste obligatoire")
                    no = nombre(o["objectif"], "objectif")
                    if no <= px:
                        raise ValueError("objectif sous le cours")
                v = o.get("suiveur_pct")
                if v is not None:
                    v = nombre(v, "suiveur")
                    if not (0.3 <= v <= 15):
                        raise ValueError("suiveur entre 0,3 et 15 %")
                # tout est vérifié avant de modifier la ligne : un ajustement refusé ne change rien
                if "stop" in o:
                    p["stop"] = r4(ns)
                if "objectif" in o:
                    p["objectif"] = r4(no)
                if "suiveur_pct" in o:
                    p["suiveur_pct"] = v
                    p["plus_haut"] = max(p.get("plus_haut") or px, px)
                ordres.append({"ts": iso(), "sens": "ajustement", "type": "ajustement", "ticker": t, "nom": p["nom"], "stop": p.get("stop"),
                               "objectif": p.get("objectif"), "suiveur_pct": p.get("suiveur_pct"), "motif": motif})
            elif act == "achat":
                if not s:
                    raise ValueError(t + " absent du scan")
                if not s["ouvert"]:
                    raise ValueError(t + " n'est pas coté en ce moment")
                f = s.get("fenetre") or {}
                if not (f.get("entree_de", 0) <= now <= f.get("entree_a", 0)):
                    raise ValueError("hors fenêtre d'entrée (%s–%s, heure de Paris)" % (hhmm(f["entree_de"]), hhmm(f["entree_a"])) if f else "fenêtre inconnue")
                px = s["cours"] * (1 + GLISSEMENT)
                obj = o.get("objectif")
                if obj is None and o.get("r"):
                    stop0 = o.get("stop")
                    obj = haut_centime(px + nombre(o["r"], "r") * (px - nombre(stop0, "stop"))) if stop0 is not None else None
                # si la dernière barre est encore en cours, la ligne la revoit au passage suivant, mais seulement
                # pour ce qui s'est passé après l'achat (plus haut / plus bas déjà faits retenus dans barre_partielle)
                br = s.get("barre") or {}
                extra = {"vu_ts": s["ts"]}
                if br and not br.get("fini"):
                    extra = {"vu_ts": s["ts"] - BARRE,
                             "barre_partielle": {"ts": br["ts"], "h": br["h"], "l": br["l"], "px": s["cours"]}}
                ouvrir(compte, t, s["nom"], s["devise"], px, o.get("stop"), obj, o.get("suiveur_pct"), o.get("montant"), fx,
                       now, motif, "marché", ordres, extra=extra)
            elif act == "cassure":
                if not s:
                    raise ValueError(t + " absent du scan")
                if p or any(x["ticker"] == t for x in compte["attente"]):
                    raise ValueError("déjà une ligne ou un ordre en attente sur " + t)
                if compte.get("coupe_circuit"):
                    raise ValueError("coupe-circuit actif (−5 % sur la journée)")
                if len(compte["attente"]) >= MAX_TRADES_JOUR - j["entrees"]:
                    raise ValueError("pas plus d'ordres en attente que d'entrées restantes aujourd'hui (%d)" % (MAX_TRADES_JOUR - j["entrees"]))
                f = s.get("fenetre") or {}
                if not f or now >= f["entree_a"]:
                    raise ValueError("fenêtre d'entrée fermée pour aujourd'hui")
                niveau = o.get("niveau", "orb")
                stop = o.get("stop", "or-mid" if niveau == "orb" else None)
                if niveau == "orb":
                    st = (s.get("orb") or {}).get("statut")
                    if st == "déclenché":
                        raise ValueError("le range a déjà cassé à %s : entrer maintenant est un autre trade (achat au marché ou cassure de niveau)"
                                         % hhmm(s["orb"]["dec_ts"] + BARRE))
                    if st in ("expiré", "données incomplètes"):
                        raise ValueError("range d'ouverture " + st)
                    if stop not in ("or-mid", "or-low"):
                        stop = nombre(stop, "stop")
                else:
                    niveau = nombre(niveau, "niveau")
                    if s["cours"] >= niveau:
                        raise ValueError("le cours (%.4f) est déjà au-dessus du niveau : utiliser un achat au marché" % s["cours"])
                    if stop is None:
                        raise ValueError("stop obligatoire sous le niveau %.4f" % niveau)
                    stop = nombre(stop, "stop")
                    if not (0 < stop < niveau):
                        raise ValueError("stop obligatoire sous le niveau %.4f" % niveau)
                r, obj = o.get("r"), o.get("objectif")
                if obj is None:
                    r = nombre(r if r is not None else 2, "r")
                    if not (GAIN_RISQUE_MIN <= r <= 5):
                        raise ValueError("r entre %.1f et 5" % GAIN_RISQUE_MIN)
                else:
                    obj = nombre(obj, "objectif")
                montant = o.get("montant")
                if montant:
                    montant = nombre(montant, "montant")
                    if montant <= 0:
                        raise ValueError("montant invalide : %r" % montant)
                sv = o.get("suiveur_pct")
                if sv is not None:
                    sv = nombre(sv, "suiveur")
                    if not (0.3 <= sv <= 15):
                        raise ValueError("suiveur entre 0,3 et 15 %")
                compte["attente"].append({"ticker": t, "nom": s["nom"], "niveau": niveau, "stop": stop, "r": r if obj is None else None,
                                          "objectif": obj, "suiveur_pct": sv,
                                          "montant": montant if montant else None, "jour": today.isoformat(),
                                          "cree_ts": now, "motif": motif})
                deposes.append("ordre cassure %s (%s, stop %s, %s)" % (
                    t, "range d'ouverture" if niveau == "orb" else "niveau %s" % niveau, stop,
                    ("objectif %s" % obj) if obj is not None else "objectif %gR" % r))
            elif act == "annuler":
                avant = len(compte["attente"])
                compte["attente"] = [x for x in compte["attente"] if x["ticker"] != t]
                if len(compte["attente"]) == avant:
                    raise ValueError("aucun ordre en attente sur " + t)
                evenements.append("cassure %s annulée par l'agent" % t)
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
            "ordres": [("%s %s %s" % (x["sens"], x.get("qte", ""), x["ticker"])).replace("  ", " ") for x in ordres] + deposes,
            "rejets": evenements + [r["raison"] for r in rejets]}
    compte["notes"] = ([note] + (compte.get("notes") or []))[:40]
    compte["titre"] = titre
    compte["statut"] = "ok"
    compte["maj"] = iso()
    dump(compte, os.path.join(a.out, "compte.json"))
    print("valeur %.2f € (%+.2f %% depuis le départ) | espèces %.2f € | lignes : %s | cassures en attente : %s" % (
        compte["valeur"], 100 * (compte["valeur"] / compte["depart"] - 1), compte["cash"],
        ", ".join("%d %s" % (p["qte"], p["ticker"]) for p in compte["positions"]) or "aucune",
        ", ".join(x["ticker"] for x in compte["attente"]) or "aucune"))
    for x in deposes:
        print("DÉPOSÉ :", x)
    for e in evenements:
        print("INFO :", e)
    for x in ordres:
        print("ORDRE :", x["sens"], x.get("qte", ""), x["ticker"], x.get("prix", ""), x.get("devise", ""),
              ("→ %+.2f €" % x["pnl_eur"]) if "pnl_eur" in x else "",
              ("| perte max %.2f € | taille : %s" % (x["perte_max_eur"], x["limite"])) if "limite" in x else "")
    for r in rejets:
        print("REJETÉ :", r["ordre"].get("action"), r["ordre"].get("ticker"), "—", r["raison"])


# ---------------------------------------------------------------- Time Machine
def pool_seances(ch):
    """Séances complètes avant aujourd'hui : {jour: barres}, nombre de barres d'une séance complète."""
    jours = {}
    for b in ch["bars"]:
        jours.setdefault(jour_paris(b["ts"]), []).append(b)
    passes = [d for d in sorted(jours) if d < aujourdhui()]
    if not passes:
        return {}, 0
    n = Counter(len(jours[d]) for d in passes).most_common(1)[0][0]
    return {d: jours[d] for d in passes if len(jours[d]) == n}, n


def historique_orb(pool, r, regle_stop):
    """Rejoue la règle sur chaque séance passée : range 15 min, première clôture au-dessus, stop, objectif r×R, à plat."""
    n_or = OR_MIN * 60 // BARRE
    st = {"seances": len(pool), "declenche": 0, "objectif": 0, "stop": 0, "aucun": 0, "refuse": 0, "R": []}
    for d, bars in pool.items():
        s, e = bars[0]["ts"], bars[-1]["ts"] + BARRE
        rg = bars[:n_or]
        hi, lo = max(b["h"] for b in rg), min(b["l"] for b in rg)
        stop0 = bas_centime((hi + lo) / 2 if regle_stop == "or-mid" else lo)
        k = next((i for i in range(n_or, len(bars)) if bars[i]["ts"] + BARRE <= e - ENTREE_AVANT * 60 and bars[i]["c"] > hi), None)
        if k is None:
            continue
        st["declenche"] += 1
        entree = bars[k]["c"]
        if not (entree * 0.85 <= stop0 <= entree * 0.995):
            st["refuse"] += 1
            continue
        risque = entree - stop0
        obj = entree + r * risque
        res = None
        for b in bars[k + 1:]:
            if b["l"] <= stop0 or b["o"] <= stop0:
                res = ("stop", -1.0)
                break
            if b["h"] >= obj or b["o"] >= obj:
                res = ("objectif", r)
                break
            if b["ts"] + BARRE >= e - A_PLAT * 60:
                res = ("aucun", (b["c"] - entree) / risque)
                break
        if res is None:
            res = ("aucun", (bars[-1]["c"] - entree) / risque)
        st[res[0]] += 1
        st["R"].append(res[1])
    return st


CAS_MIN = 15  # en dessous, pas de verdict : trop peu de séances pour distinguer une tendance du hasard


def verdict(pt, ps, r, quoi="séances", n_info=None):
    """Part « objectif d'abord » parmi les cas qui touchent un côté, comparée au point mort 1/(1+r).
    La précision dépend du nombre de séances réelles d'où viennent les données (n_info), pas du nombre de rejeux :
    tant que le point mort reste dans l'intervalle de confiance à 95 % (Wilson), pas de conclusion."""
    touche = pt + ps
    if touche <= 0:
        return "Ni le stop ni l'objectif ne sont touchés : tout se joue au prix de sortie."
    p = pt / touche
    pm = 1 / (1 + r)
    n = int(n_info) if n_info is not None else touche
    if n < CAS_MIN:
        return "Pas de conclusion : %d %s seulement, il en faut %d (objectif d'abord %.0f %% ; point mort %.0f %%)" % (
            n, quoi, CAS_MIN, 100 * p, 100 * pm)
    z = 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    lo, hi = c - h, c + h
    detail = " (objectif d'abord dans %.1f %% des cas qui touchent un côté, marge %.0f–%.0f %% sur %d %s ; point mort %.0f %%)" % (
        100 * p, 100 * lo, 100 * hi, n, quoi, 100 * pm)
    if lo <= pm <= hi:
        return "Pas de conclusion : l'écart au point mort reste dans la marge d'erreur" + detail
    ec = 100 * (p - pm)
    cote = "l'objectif" if ec > 0 else "le stop"
    return ("Penche vers " if abs(ec) < 15 else "Nettement vers ") + cote + detail


def cmd_tm(a):
    ch = chart(a.ticker, "60d")
    if not ch:
        sys.exit("ERREUR : pas de barres de 5 min pour " + a.ticker)
    now = time.time()
    pool, n_full = pool_seances(ch)
    if len(pool) < 20:
        sys.exit("REFUS : %d séances complètes seulement dans les 60 derniers jours (20 au minimum)" % len(pool))
    today = aujourdhui()
    f = fenetre(ch, today)
    ro = range_ouverture(ch, today, now)
    dev = ch["devise"]
    print("TIME MACHINE %s — %d séances complètes de %d barres (Yahoo, 5 min, ~60 jours), graine %d" % (a.ticker, len(pool), n_full, a.seed))
    entree = stop = obj = None
    debut_ts = None
    if a.orb:
        r = float(a.r)
        h = historique_orb(pool, r, a.regle_stop)
        rm = sum(h["R"]) / len(h["R"]) if h["R"] else 0
        print("Historique de la règle (range %d min, première clôture au-dessus, stop %s, objectif %gR, à plat %d min avant la clôture) :"
              % (OR_MIN, "milieu du range" if a.regle_stop == "or-mid" else "bas du range", r, A_PLAT))
        print("  déclenchée %d fois sur %d séances — objectif %d, stop %d, aucun %d ; %d refusées par le moteur (stop hors 0,5–15 %%) ; R moyen %+.2f"
              % (h["declenche"], h["seances"], h["objectif"], h["stop"], h["aucun"], h["refuse"], rm))
        ht = h["objectif"] + h["stop"]
        if ht:
            print("  " + verdict(h["objectif"], h["stop"], r, "séances"))
        if h["R"]:
            et = (sum((x - rm) ** 2 for x in h["R"]) / max(1, len(h["R"]) - 1)) ** 0.5
            print("  R moyen %+.2f ± %.2f (marge à 95 %% sur %d trades)" % (rm, 1.96 * et / math.sqrt(len(h["R"])), len(h["R"])))
        print("Aujourd'hui : " + texte_orb(ro, ch["last"]))
        if ro.get("statut") != "déclenché":
            return
        entree = ro["dec_px"]
        stop = bas_centime(ro["mid"] if a.regle_stop == "or-mid" else ro["bas"])
        obj = entree + r * (entree - stop)
        debut_ts = ro["dec_ts"] + BARRE
    else:
        if a.stop is None or a.objectif is None:
            sys.exit("ERREUR : --stop et --objectif obligatoires (ou --orb)")
        entree = float(a.entree) if a.entree else ch["last"]
        stop, obj = float(a.stop), float(a.objectif)
        debut_ts = max(now, f["debut"])
    if not (stop < entree < obj):
        sys.exit("REFUS : il faut stop < entrée < objectif (%.4f / %.4f / %.4f)" % (stop, entree, obj))
    s0 = f["debut"]
    premier = int(math.ceil((debut_ts - s0) / BARRE))
    dernier = int((f["a_plat"] - s0) // BARRE) - 1
    if premier > dernier:
        sys.exit("REFUS : la séance se termine avant une barre complète (à plat à %s)" % hhmm(f["a_plat"]))
    # ratios de chaque barre à la clôture précédente, par position dans la séance
    jours = sorted(pool)
    ratios = []
    tous = ch["bars"]
    idx = {b["ts"]: i for i, b in enumerate(tous)}
    for d in jours:
        rr = []
        for b in pool[d]:
            i = idx[b["ts"]]
            pc = tous[i - 1]["c"] if i > 0 else b["o"]
            rr.append((b["o"] / pc, b["h"] / pc, b["l"] / pc, b["c"] / pc))
        ratios.append(rr)
    rnd = random.Random(a.seed)
    risque = entree - stop
    R = (obj - entree) / risque
    cpt = {"objectif": 0, "stop": 0, "aucun": 0}
    vert, finals, Rs = 0, [], []
    for _ in range(a.n):
        p = entree
        res = None
        for sl in range(premier, dernier + 1):
            rr = ratios[rnd.randrange(len(ratios))]
            j2 = min(max(sl + rnd.randint(-2, 2), 0), len(rr) - 1)
            ro_, rh, rl, rc = rr[j2]
            o_, h_, l_, c_ = p * ro_, p * rh, p * rl, p * rc
            if o_ <= stop or l_ <= stop:
                res, fin, rv = "stop", min(o_, stop), -1.0
                break
            if o_ >= obj or h_ >= obj:
                res, fin, rv = "objectif", max(o_, obj), R
                break
            p = c_
        if res is None:
            res, fin, rv = "aucun", p, (p - entree) / risque
        cpt[res] += 1
        finals.append(fin)
        Rs.append(rv)
        if res == "objectif" or (res == "aucun" and fin > entree):
            vert += 1
    finals.sort()
    N = a.n
    pc = lambda k: 100.0 * cpt[k] / N
    print("Plan : entrée %.4f %s, stop %.4f (−%.2f %%), objectif %.4f (%.2fR) | rejeux de %s à %s (Paris, à plat)" % (
        entree, dev, stop, 100 * risque / entree, obj, R, hhmm(s0 + premier * BARRE), hhmm(f["a_plat"])))
    print("%d rejeux : objectif d'abord %.1f %% | stop d'abord %.1f %% | aucun à l'heure de sortie %.1f %% | au-dessus de l'entrée %.1f %%"
          % (N, pc("objectif"), pc("stop"), pc("aucun"), 100.0 * vert / N))
    print("Prix à la sortie : médiane %.4f | 5–95 %% : %.4f – %.4f | R moyen %+.2f" % (
        finals[N // 2], finals[int(0.05 * N)], finals[int(0.95 * N) - 1], sum(Rs) / N))
    # chaque rejeu recompose des barres de ces séances : l'information vient des séances, pas des 10 000 tirages
    n_info = round(len(pool) * (cpt["objectif"] + cpt["stop"]) / N)
    v = verdict(cpt["objectif"], cpt["stop"], R, "séances d'origine", n_info)
    print("Verdict : " + v)
    print("Précision : limitée par les %d séances réelles d'origine, pas par les %d rejeux." % (len(pool), N))
    if pc("aucun") > 40:
        print("Plus de 40 % des rejeux ne touchent ni le stop ni l'objectif : le prix à l'heure de sortie décide.")
    rm = sum(Rs) / N
    if ("vers l'objectif" in v and rm < 0) or ("vers le stop" in v and rm > 0):
        print("Attention : le R moyen (%+.2f) va dans l'autre sens, à cause des rejeux qui ne touchent rien." % rm)
    print("Simulation tirée des 60 derniers jours, pas une prévision : résultats, annonces ou suspensions peuvent faire autre chose.")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("init"); x.add_argument("--out", required=True)
    x = sp.add_parser("pas"); x.add_argument("--compte", required=True); x.add_argument("--out", required=True)
    x = sp.add_parser("appliquer"); x.add_argument("--decisions", required=True); x.add_argument("--out", required=True)
    x = sp.add_parser("tm")
    x.add_argument("--ticker", required=True); x.add_argument("--entree", type=float); x.add_argument("--stop", type=float)
    x.add_argument("--objectif", type=float); x.add_argument("--orb", action="store_true"); x.add_argument("--r", type=float, default=2.0)
    x.add_argument("--regle-stop", dest="regle_stop", choices=["or-mid", "or-low"], default="or-mid")
    x.add_argument("--n", type=int, default=10000); x.add_argument("--seed", type=int, default=19)
    a = ap.parse_args()
    {"init": cmd_init, "pas": cmd_pas, "appliquer": cmd_appliquer, "tm": cmd_tm}[a.cmd](a)


if __name__ == "__main__":
    main()
