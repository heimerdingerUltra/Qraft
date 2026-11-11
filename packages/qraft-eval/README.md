# qraft-eval

Harnais d'évaluation de Qraft pour la phase 0 : jeux or, précision d'exécution et baseline.

```bash
pip install -e "packages/qraft-eval[baseline,duckdb,dev]"
qraft-eval --help
```

| Commande | Rôle |
| --- | --- |
| `qraft-eval demo-db` | Crée la base de démonstration DuckDB |
| `qraft-eval validate <jeu.yaml>` | Vérifie que chaque référence s'exécute et renvoie des lignes |
| `qraft-eval schema` | Affiche le schéma exactement tel qu'il sera envoyé au modèle |
| `qraft-eval coverage <jeu.yaml>` | Couverture QIR v0 d'après les annotations |
| `qraft-eval baseline <jeu.yaml> --dry-run` | Estime le coût de la baseline sans appeler le modèle |
| `qraft-eval baseline <jeu.yaml>` | Mesure la baseline et écrit le rapport |

La base se désigne par `--db-url` ou par la variable `QRAFT_DB_URL` (URL SQLAlchemy). Le protocole complet est dans [phase0/PHASE0.md](../../phase0/PHASE0.md).
