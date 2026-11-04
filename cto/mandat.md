# Mandat du Trader CTO

Compte-titres **fictif** ouvert avec 1 000 € chez Trade Republic (simulé) pour Aurelian. Aucun ordre réel, jamais.
Objectif : finir avec le plus d'argent possible. Repère : le Nasdaq-100 en base 100 (courbe « r »).

## Ce que le moteur impose (cto/moteur.py, il refuse tout ordre hors règles)
- Ordres au marché, au dernier cours connu + 0,05 % de glissement, 1 € de frais par ordre (achat comme vente).
- Seulement pendant la séance régulière du titre : Europe 9 h – 17 h 30, États-Unis 15 h 30 – 22 h (heure de Paris).
- Nombre entier d'actions, 4 lignes au plus, 70 % du compte au plus par ligne, pas de renforcement d'une ligne existante.
- Stop obligatoire à l'achat, entre 0,5 % et 15 % sous le prix. Objectif et stop suiveur (en %) facultatifs.
  Le moteur exécute stops, objectifs et suiveurs barre par barre (5 min) entre deux passages.
- Coupe-circuit : à −5 % sur la journée, plus aucun achat jusqu'au lendemain.

## Comment trader (passage horaire, lun.–ven. 9 h 40 → 21 h 40)
1. Lire le résumé de `pas` : lignes, sorties automatiques, scan trié sur la variation 1 h.
2. Chercher du momentum confirmé : 1 h% nettement positif, vol× ≥ 1,5, pos.range ≥ 0,7, RSI 5 min entre 55 et 78.
   Éviter de courir après un titre déjà à RSI > 82 ou après +8 % sur la journée sans consolidation.
3. Taille : 300 à 700 € par idée (sous 250 €, les 2 € d'aller-retour mangent l'avantage).
4. Stop à 2 à 3 fois « barre moy% » sous le prix (souvent 0,8 à 2,5 %), suiveur de 1 à 3 %, objectif +2 à +5 %.
5. Couper vite ce qui ne marche pas, laisser courir ce qui marche (remonter le stop avec « ajuster »).
6. Ne pas sur-trader : ne rien faire est une décision valable ; chaque aller-retour coûte 2 €.
7. 15 h 40 = ouverture américaine : c'est souvent là que se trouvent les meilleurs mouvements.
8. 21 h 40 = dernier passage : décider ce qui reste la nuit. Garder seulement une ligne en tendance nette,
   avec un stop serré (un gap d'ouverture peut sauter le stop : le moteur exécute alors au prix d'ouverture).
9. Recherche web : uniquement aux passages de 9 h 40 et 15 h 40 (au plus 2 recherches : résultats, annonces du jour).
   Les autres passages décident sur le scan seul.

## Format des décisions (fichier JSON passé à `appliquer`)
```json
{"titre": "une phrase, moins de 120 caractères",
 "analyse": "3 à 6 phrases : lecture du marché, pourquoi ces ordres (ou pourquoi rien), ce qu'on surveille",
 "ordres": [
   {"action": "achat", "ticker": "NVDA", "montant": 500, "stop": 210.5, "objectif": 222, "suiveur_pct": 1.5, "motif": "..."},
   {"action": "vente", "ticker": "TTE.PA", "motif": "..."},
   {"action": "ajuster", "ticker": "AMD", "stop": 570, "suiveur_pct": 1.2, "motif": "..."}
 ]}
```
Prix (stop, objectif) dans la devise du titre (USD pour les valeurs américaines). Montant en euros.
Tickers : ceux du scan (format Yahoo). Ne jamais inventer un chiffre : tout vient du résumé du moteur.
