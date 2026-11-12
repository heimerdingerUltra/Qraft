# Qraft

Plateforme interne d'intelligence de données : une question en français, une réponse chiffrée, vérifiée et expliquée, sur n'importe quelle base SQL de l'entreprise.

Le modèle ne produit jamais de SQL final : il produit une intention typée (QIR) qu'un compilateur traduit en SQL pour le dialecte de la base, avec des jointures calculées sur un graphe de connaissance, des politiques de sécurité appliquées sur l'AST, une vérification du résultat et un score de confiance. Quand il n'est pas sûr, Qraft le dit.

## Démarrage

```bash
python -m venv ~/.venvs/qraft && . ~/.venvs/qraft/bin/activate   # Windows : chemin court obligatoire
pip install -e "packages/qraft-eval[duckdb]" -e "packages/core[duckdb,arrow]" -e "packages/server[mcp,arrow]"
qraft-eval demo-db
qraft ask "Chiffre d'affaires 2025 par région, du plus fort au plus faible" --db duckdb:///phase0/demo/ventes.duckdb --semantic examples/ventes.semantic.yaml --sql
qraft shell --db duckdb:///phase0/demo/ventes.duckdb --semantic examples/ventes.semantic.yaml
qraft serve --db duckdb:///phase0/demo/ventes.duckdb --semantic examples/ventes.semantic.yaml
```

```python
from qraft import Engine

engine = Engine.from_url("duckdb:///phase0/demo/ventes.duckdb", semantic="examples/ventes.semantic.yaml")
answer = engine.ask("Chiffre d'affaires 2025 par région")
print(answer.interpretation, answer.confidence.tier)
print(answer.follow_up("et par mois ?").rows[:3])
```

## Contenu

| Où | Quoi |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Spécification d'architecture |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | Ce qui est construit, ce qui reste à faire |
| [packages/core](packages/core) | Moteur `qraft` : QIR, graphe, compilateur, parseur, vérification, confiance, fédération, CLI |
| [packages/server](packages/server) | API REST/SSE, export Arrow, serveur MCP |
| [packages/forge](packages/forge) | Forge : données d'entraînement vérifiées, récompense d'exécution, entraînement LoRA/GRPO |
| [packages/qraft-eval](packages/qraft-eval) | Harnais d'évaluation (baseline Claude, moteur) |
| [phase0/PHASE0.md](phase0/PHASE0.md) | Protocole de mesure sur un domaine pilote |
| [examples/ventes.semantic.yaml](examples/ventes.semantic.yaml) | Couche sémantique de démonstration |
| [deploy](deploy) | Dockerfile et docker-compose de démonstration |
