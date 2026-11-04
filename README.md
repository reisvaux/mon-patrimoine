# Mon Patrimoine

App personnelle de suivi de patrimoine (PEA BNP, assurance-vie, livrets, épargne salariale, Bitcoin), publiée comme artifact Claude.

- `index.html` : l'app complète (une page, sans dépendance). Les données vivent dans la base de l'artifact, pas dans ce dépôt.
- `outils/live.py` : mise à jour des cours en séance (tâche planifiée « Mon Patrimoine — cours en direct »).
- `cto/` : moteur et mandat du Trader CTO (compte fictif de 1 000 €).
- `outils/verifier.py` : vérifications lancées à chaque push par GitHub Actions (syntaxe JS, scripts Python, règles de la page).

Aucune donnée financière personnelle n'est versionnée ici.
