#!/usr/bin/env python3
"""Mise à jour en direct de l'app « Mon Patrimoine » : cours du PEA et du Bitcoin, score du duel agent trader vs PEA.

Usage : python3 live.py --app <portfolio.json> --duel <dossier> --out <dossier>
  --app   : document portfolio/main de l'app (fichier écrit par ArtifactData get/out_dir)
  --duel  : copie de la base du duel : <dossier>/etat/{agent,reel,config}.json, <dossier>/nav/*.json,
            <dossier>/mouvements/*.json (dossier facultatif)
  --out   : écrit portfolio-update.json (accounts, pricesAt, history) et duel-courbe.json (document duel/courbe de l'app :
            points base 100 des clôtures officielles + point en direct, titre et sections affichés dans l'onglet Agents)

Cours : Yahoo Finance, graphique du jour au pas de 5 minutes (différé d'environ 15 minutes pour Euronext Paris).
Le score reprend exactement les règles de moteur.py (page du duel) : ordres de l'agent exécutés sur la séance en cours
(provisoire), mouvements d'Aurelian appliqués au PEA réel, versements ajoutés aux deux portefeuilles et neutralisés
dans la base 100. Le passage du soir (23 h 10) reste la référence officielle.
"""
import argparse, glob, json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover
    PARIS = timezone(timedelta(hours=1))

DUEL_URL = "https://claude.ai/artifact/1PuB2483xJWJGtaBu9isvu"
UA = {"User-Agent": "Mozilla/5.0"}
NB = " "


def load(p, default=None):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except FileNotFoundError:
        return default
    return d["data"] if isinstance(d, dict) and "data" in d and "id" in d else d


def get(url, tries=3):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.load(r)
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 + 2 * k)


def intraday(t):
    """Séance en cours (ou dernière séance) : veille, barres de 5 min, dernier cours et son heure."""
    try:
        res = get("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(t) + "?range=1d&interval=5m")["chart"]["result"][0]
    except Exception:
        return None
    meta = res["meta"]
    q = res["indicators"]["quote"][0]
    pts = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        pts.append({"ts": ts, "o": q["open"][i] if q["open"][i] is not None else c,
                    "h": q["high"][i] if q["high"][i] is not None else c, "l": q["low"][i] if q["low"][i] is not None else c, "c": c})
    time.sleep(0.25)
    if not pts:
        return None
    return {"prev": meta.get("chartPreviousClose") or meta.get("previousClose"), "pts": pts, "last": pts[-1]["c"], "ts": pts[-1]["ts"],
            "day": datetime.fromtimestamp(pts[0]["ts"], PARIS).date().isoformat(),
            "bar": {"open": pts[0]["o"], "high": max(x["h"] for x in pts), "low": min(x["l"] for x in pts), "close": pts[-1]["c"]},
            "name": meta.get("longName") or meta.get("shortName") or t}


def r2(x):
    return round(float(x) + 0.0, 2)


def eur(x):
    s = f"{abs(x):,.2f}".replace(",", " ").replace(".", ",")
    return ("−" if x < -0.004 else "") + s + NB + "€"


def seur(x):
    return ("+" if x >= 0 else "") + eur(x)


def pct(x, d=2):
    return ("+" if x >= 0 else "−") + f"{abs(x):.{d}f}".replace(".", ",") + NB + "%"


def num(x, d=2):
    return f"{x:.{d}f}".replace(".", ",")


def pts_(x):
    return ("+" if x >= 0 else "−") + num(abs(x)) + NB + "pt"


def tone(x, eps=0.0):
    return "up" if x > eps else "down" if x < -eps else "flat"


def ddmm(d):
    return datetime.fromisoformat(d[:10]).strftime("%d/%m")


# --- règles d'exécution recopiées de moteur.py (remplir / executer / appliquer_mouvement_reel) ---
def remplir(o, bar):
    op, hi, lo = bar["open"], bar["high"], bar["low"]
    typ, L, sens = o["type"], o.get("limite"), o["sens"]
    if typ == "marche":
        return op
    if typ == "limite":
        if sens == "achat":
            return op if op <= L else (L if lo <= L else None)
        return op if op >= L else (L if hi >= L else None)
    if typ == "stop":
        if sens == "vente":
            return op if op <= L else (L if lo <= L else None)
        return op if op >= L else (L if hi >= L else None)
    return None


def executer(o, bar, pos, cash, courtage, ttf_rate):
    """Retourne (statut, prix, quantité, montant signé pour le flux de la ligne, frais, cash)."""
    if bar is None:
        return "pas de cotation sur la séance", None, 0, 0.0, 0.0, cash
    prix = remplir(o, bar)
    if prix is None:
        return "non atteint", None, 0, 0.0, 0.0, cash
    prix = round(prix, 4)
    q, t = int(o["qte"]), o["ticker"]
    if o["sens"] == "vente":
        held = pos.get(t, {}).get("qte", 0)
        q = min(q, held)
        if q <= 0:
            return "rejeté (aucun titre)", None, 0, 0.0, 0.0, cash
        brut = q * prix
        frais = brut * courtage
        cash += brut - frais
        pos[t]["qte"] = held - q
        if pos[t]["qte"] == 0:
            del pos[t]
        return "exécuté", prix, q, brut - frais, frais, cash
    taux = courtage + (ttf_rate if o.get("ttf") else 0)
    if q * prix * (1 + taux) > cash + 1e-9:
        q = int(cash // (prix * (1 + taux)))
        if q <= 0:
            return "rejeté (cash insuffisant)", None, 0, 0.0, 0.0, cash
    brut = q * prix
    frais = brut * courtage + (brut * ttf_rate if o.get("ttf") else 0.0)
    cash -= brut + frais
    p = pos.get(t)
    if p:
        p["qte"] += q
    else:
        pos[t] = {"ticker": t, "nom": o.get("nom") or t, "qte": q}
    return "exécuté", prix, q, -(brut + frais), frais, cash


def appliquer_mouvement_reel(m, pos, cash):
    t = m.get("ticker")
    if m["type"] == "achat":
        q = int(m["qte"])
        if t in pos:
            pos[t]["qte"] += q
        else:
            pos[t] = {"ticker": t, "nom": m.get("nom") or t, "qte": q}
        cash -= float(m["montant"])
    elif m["type"] == "vente":
        q = int(m["qte"])
        if t in pos:
            pos[t]["qte"] -= q
            if pos[t]["qte"] <= 0:
                del pos[t]
        cash += float(m["montant"])
    return cash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--duel", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    now = datetime.now(PARIS)
    today = now.date().isoformat()

    port = load(a.app)
    agent = load(os.path.join(a.duel, "etat", "agent.json"))
    reel = load(os.path.join(a.duel, "etat", "reel.json"))
    cfg = load(os.path.join(a.duel, "etat", "config.json"))
    duel_ok = bool(a.duel) and agent is not None and reel is not None and cfg is not None
    if not duel_ok:  # duel supprimé : seuls les cours sont mis à jour
        agent, reel, cfg = {}, {}, {}
    nav = sorted((load(f) for f in glob.glob(os.path.join(a.duel, "nav", "*.json"))), key=lambda x: x["date"])
    mouvs = []
    for f in sorted(glob.glob(os.path.join(a.duel, "mouvements", "*.json"))):
        m = load(f)
        m["id"] = os.path.splitext(os.path.basename(f))[0]
        mouvs.append(m)

    accounts = port["accounts"]
    pea = next((x for x in accounts if x["id"] == "pea"), None)
    btc_acc = next((x for x in accounts if x["id"] == "btc"), None)

    tickers = {"^FCHI", "WPEA.PA"}
    if pea:
        tickers |= {p["ticker"] + ".PA" for p in pea.get("positions", [])}
    tickers |= {p["ticker"] for p in agent.get("positions", []) + reel.get("positions", [])}
    tickers |= {o["ticker"] for o in agent.get("ordres_en_attente", [])}
    tickers |= {m["ticker"] for m in mouvs if m.get("ticker")}
    I = {t: intraday(t) for t in sorted(tickers)}
    btc = intraday("BTC-EUR") if btc_acc else None
    cac = I.get("^FCHI")
    seance = bool(cac) and cac["day"] == today

    # ---------- 1. cours dans l'app ----------
    changed, missing = [], []

    def stamp(ts):
        d = datetime.fromtimestamp(ts, PARIS)
        return d.date().isoformat(), d.isoformat(timespec="minutes")
    if pea:
        for p in pea.get("positions", []):
            q = I.get(p["ticker"] + ".PA")
            if not q:
                missing.append(p["ticker"]); continue
            day, iso = stamp(q["ts"])
            if day < (p.get("priceAt") or ""):
                continue
            if q["prev"]:
                p["prevPrice"] = round(q["prev"], 4)
            p["price"] = round(q["last"], 4)
            p["priceAt"], p["priceTime"] = day, iso
            changed.append(p["ticker"])
    if btc_acc:
        for p in btc_acc.get("positions", []):
            if p.get("ticker") == "BTC":
                if not btc:
                    missing.append("BTC"); continue
                day, iso = stamp(btc["ts"])
                if btc["prev"]:
                    p["prevPrice"] = round(btc["prev"], 2)
                p["price"], p["priceAt"], p["priceTime"] = round(btc["last"], 2), day, iso
                changed.append("BTC")

    def accval(x):
        if x["type"] == "titres":
            return sum(p["qty"] * p["price"] for p in x.get("positions", [])) + x.get("cash", 0)
        return x.get("value", 0)
    upd = {"accounts": accounts, "pricesAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    if not port.get("isExample"):
        hist = [e for e in port.get("history", []) if e.get("d") != today]
        entry = {"d": today, "t": r2(sum(accval(x) for x in accounts)), "a": {x["id"]: r2(accval(x)) for x in accounts}}
        c = {x["id"]: r2(x["contrib"]) for x in accounts if (x.get("contrib") or 0) > 0}
        if c:
            entry["c"] = c
        hist.append(entry)
        hist.sort(key=lambda e: e["d"])
        upd["history"] = hist
    json.dump(upd, open(os.path.join(a.out, "portfolio-update.json"), "w"), ensure_ascii=False)

    if not duel_ok:
        print(json.dumps({"seance_en_cours": seance, "cours_mis_a_jour": changed, "cours_introuvables": missing,
                          "titre": "Duel supprimé : cours mis à jour, aucune courbe écrite (n'écris que portfolio/main)",
                          "total": upd.get("history", [{}])[-1].get("t")}, ensure_ascii=False))
        return

    # ---------- 2. score du duel ----------
    frais = cfg.get("frais", {})
    courtage, ttf_rate = float(frais.get("courtage_agent", 0.01)), float(frais.get("ttf", 0.004))
    # mêmes points que l'étape 4 ter du passage du soir (une clôture officielle par séance)
    points = [{"d": n["date"], "agent": n["b_agent"], "reel": n["b_reel"], "wpea": n["b_wpea"], "cac": n["b_cac"],
               "va": n["agent"], "vr": n["reel"]} for n in nav]
    start = ddmm(cfg.get("depart_date") or (points[0]["d"] if points else today))
    card = {"source": DUEL_URL + " (collection nav) + cours Yahoo Finance du jour", "ranAt": now.isoformat(timespec="seconds"), "live": False}

    def lead(ba, br):
        e = round(ba, 2) - round(br, 2)
        return "égalité parfaite" if abs(e) < 0.005 else ("l'agent mène de " if e > 0 else "le PEA mène de ") + num(abs(e)) + NB + "pt"

    app_names = {p["ticker"] + ".PA": p.get("name") for p in (pea or {}).get("positions", []) if p.get("name")}
    since_txt = None
    last_nav = nav[-1] if nav else None
    if seance and last_nav and last_nav["date"] < today and agent.get("seance") == last_nav["date"]:
        apos = {p["ticker"]: {"ticker": p["ticker"], "nom": p.get("nom"), "qte": p["qte"]} for p in agent.get("positions", [])}
        rpos = {p["ticker"]: {"ticker": p["ticker"], "nom": p.get("nom"), "qte": p["qte"]} for p in reel.get("positions", [])}
        acash, rcash = float(agent["cash"]), float(reel["cash"])
        last = lambda t: (I.get(t) or {}).get("last")
        prevc = lambda t: (I.get(t) or {}).get("prev")
        start_a = {t: p["qte"] for t, p in apos.items()}
        start_r = {t: p["qte"] for t, p in rpos.items()}
        flow_a, flow_r, flux = {}, {}, 0.0
        appliques = set(reel.get("mouvements_appliques", []))
        mv_items = []
        for m in sorted([m for m in mouvs if m["id"] not in appliques and m.get("date", "9999") <= today], key=lambda m: (m.get("date", ""), m.get("cree_le", ""))):
            if m["type"] == "versement":
                amt = float(m["montant"]); flux += amt; acash += amt; rcash += amt
                mv_items.append({"label": f"Versement de {eur(amt)}", "detail": "ajouté aux deux portefeuilles et neutralisé dans la base 100"})
            elif m["type"] in ("achat", "vente"):
                rcash = appliquer_mouvement_reel(m, rpos, rcash)
                flow_r[m["ticker"]] = flow_r.get(m["ticker"], 0) + (-1 if m["type"] == "achat" else 1) * float(m["montant"])
                nom = app_names.get(m["ticker"]) or (I.get(m["ticker"]) or {}).get("name", m["ticker"])
                mv_items.append({"label": f"{'Achat' if m['type'] == 'achat' else 'Vente'} de {m['qte']} {nom} à {num(float(m['prix']), 3)}{NB}€",
                                 "detail": f"{eur(float(m['montant']))} frais compris" + (f" (dont {eur(float(m['frais']))} de frais)" if m.get("frais") else "")})
        ord_items = []
        for o in sorted([o for o in agent.get("ordres_en_attente", []) if o.get("decide_le", "") < today], key=lambda o: 0 if o["sens"] == "vente" else 1):
            q = I.get(o["ticker"])
            bar = q["bar"] if q and q["day"] == today else None
            statut, prix, qx, montant, fr, acash = executer(o, bar, apos, acash, courtage, ttf_rate)
            typ = {"stop": "stop", "limite": "à cours limité", "marche": "au marché"}.get(o["type"], o["type"])
            label = f"{'Vente' if o['sens'] == 'vente' else 'Achat'} {typ} · {o['qte']} {o.get('nom', o['ticker'])}" + (f" à {num(o['limite'])}{NB}€" if o.get("limite") else "")
            if statut == "exécuté":
                flow_a[o["ticker"]] = flow_a.get(o["ticker"], 0) + montant
                detail = f"exécuté (provisoire) : {qx} à {num(prix)}{NB}€, frais {eur(fr)}"
            elif statut == "non atteint" and q and o.get("limite"):
                detail = f"pas déclenché · cours {num(q['last'])}{NB}€, soit {pct((q['last'] / o['limite'] - 1) * 100, 1)} par rapport au seuil"
            else:
                detail = statut
            ord_items.append({"label": label, "detail": detail})
        v0a, v0r = float(agent["valeur_totale"]), float(reel["valeur_totale"])
        va = acash + sum(p["qte"] * (last(t) or 0) for t, p in apos.items())
        vr = rcash + sum(p["qte"] * (last(t) or 0) for t, p in rpos.items())
        ba = float(agent["base100"]) * va / (v0a + flux)
        br = float(reel["base100"]) * vr / (v0r + flux)
        da, dr = va / (v0a + flux) - 1, vr / (v0r + flux) - 1
        wq = I["WPEA.PA"]
        bw = 100 * wq["last"] / float(cfg["depart_wpea"])
        bc = 100 * cac["last"] / float(cfg["depart_cac"])
        hh = datetime.fromtimestamp(cac["ts"], PARIS).strftime("%H:%M")
        hh_fr = hh.replace(":", " h ")
        points.append({"d": today, "t": hh, "live": True, "agent": round(ba, 4), "reel": round(br, 4), "wpea": round(bw, 4),
                       "cac": round(bc, 4), "va": r2(va), "vr": r2(vr)})
        card["title"] = f"À {hh_fr} : agent {num(ba)} · PEA {num(br)} — {lead(ba, br)}"
        card["live"] = True
        cac_d, wp_d = cac["last"] / cac["prev"] - 1, wq["last"] / wq["prev"] - 1
        day_items = [
            {"label": "Agent " + pct(da * 100), "detail": eur(va) + " en direct", "tone": tone(round(da * 100, 2))},
            {"label": "PEA réel " + pct(dr * 100), "detail": eur(vr) + " en direct", "tone": tone(round(dr * 100, 2))},
            {"label": "CAC 40 " + pct(cac_d * 100), "detail": num(cac["last"]) + NB + "points", "tone": tone(round(cac_d * 100, 2))},
            {"label": "WPEA " + pct(wp_d * 100), "detail": num(wq["last"], 3) + NB + "€", "tone": tone(round(wp_d * 100, 2))}]
        # contribution de chaque ligne sur la séance (même calcul que le moteur : valeur fin − valeur veille + flux)
        lignes = []
        for t in set(start_a) | set(start_r) | set(apos) | set(rpos) | set(flow_a) | set(flow_r):
            q = I.get(t)
            if not q or not q["prev"]:
                continue
            ca = (apos[t]["qte"] if t in apos else 0) * q["last"] - start_a.get(t, 0) * q["prev"] + flow_a.get(t, 0)
            cr = (rpos[t]["qte"] if t in rpos else 0) * q["last"] - start_r.get(t, 0) * q["prev"] + flow_r.get(t, 0)
            nom = app_names.get(t) or (apos.get(t) or rpos.get(t) or {}).get("nom") or q["name"]
            if nom == t:
                nom = q["name"]
            dpx = q["last"] / q["prev"] - 1
            same = abs(ca - cr) < 0.005
            lignes.append((max(abs(ca), abs(cr)), {
                "label": f"{nom} {pct(dpx * 100)}",
                "detail": f"{num(q['last'], 3 if q['last'] < 50 else 2)}{NB}€ · " + (f"{seur(ca)} pour chacun" if same else f"agent {seur(ca)} · PEA {seur(cr)}"),
                "tone": tone(round(dpx * 100, 2))}))
        lignes = [x[1] for x in sorted(lignes, key=lambda x: -x[0])[:8]]
        card["sections"] = [{"title": f"Aujourd'hui, à {hh_fr}", "items": day_items},
                            {"title": "Ordres de l'agent", "items": ord_items or [{"label": "Aucun ordre en attente", "detail": "l'agent n'a rien prévu pour cette séance"}]}]
        if mv_items:
            card["sections"].append({"title": "Opérations du jour sur le PEA réel", "items": mv_items})
        card["sections"].append({"title": "Contribution des lignes aujourd'hui", "items": lignes})
        since_txt = (f"Agent {pct(ba - 100)} · PEA réel {pct(br - 100)} · WPEA {pct(bw - 100)} · CAC 40 {pct(bc - 100)}.")
    else:
        n = last_nav or {}
        if n:
            card["title"] = (f"Clôture du {ddmm(n['date'])} : agent {num(n['b_agent'])} · PEA {num(n['b_reel'])} — {lead(n['b_agent'], n['b_reel'])}"
                             if n["date"] == today else
                             f"Marché fermé · dernière clôture ({ddmm(n['date'])}) : agent {num(n['b_agent'])} · PEA {num(n['b_reel'])}")
            since_txt = (f"Agent {pct(n['b_agent'] - 100)} · PEA réel {pct(n['b_reel'] - 100)} · WPEA {pct(n['b_wpea'] - 100)} · CAC 40 {pct(n['b_cac'] - 100)}.")
        else:
            card["title"] = "Le duel n'a pas encore de clôture enregistrée"
        card["sections"] = []
        if agent.get("ordres_en_attente"):
            card["sections"].append({"title": "Ordres de l'agent pour la prochaine séance", "items": [
                {"label": f"{'Vente' if o['sens'] == 'vente' else 'Achat'} {o['type']} · {o['qte']} {o.get('nom', o['ticker'])}" + (f" à {num(o['limite'])}{NB}€" if o.get("limite") else ""),
                 "detail": o.get("motif", "")} for o in agent["ordres_en_attente"]]})
    if since_txt:
        card["sections"].append({"title": f"Depuis le départ ({start})", "text":
            since_txt + "\n"
            f"Frais payés par l'agent : {eur(float(agent.get('frais_cumules', 0)))} · ordres exécutés : {agent.get('nb_ordres_executes', 0)}.\n"
            "Base 100 = valeur au départ, versements neutralisés : c'est la seule mesure comparable entre l'agent et le PEA. "
            "En séance, le score est provisoire ; le passage du soir (23 h 10) fait foi."})
    card["points"] = points
    card["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(card, open(os.path.join(a.out, "duel-courbe.json"), "w"), ensure_ascii=False)
    lp = points[-1] if points else {}
    print(json.dumps({"seance_en_cours": seance, "cours_mis_a_jour": changed, "cours_introuvables": missing, "titre": card["title"],
                      "dernier_point": {k: lp.get(k) for k in ("d", "t", "agent", "reel", "wpea", "va", "vr")}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
