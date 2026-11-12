# qraft (moteur)

Le moteur de Qraft : il interprète une question en français, la représente en QIR (intention typée), la compile en SQL pour le dialecte de la base, l'exécute en lecture seule, vérifie le résultat et calcule une confiance.

```python
from qraft import Engine

engine = Engine.from_url("duckdb:///phase0/demo/ventes.duckdb", semantic="examples/ventes.semantic.yaml")
answer = engine.ask("Chiffre d'affaires 2025 par région, du plus fort au plus faible")
print(answer.interpretation)   # rendu en français de l'intention
print(answer.sql)              # SQL compilé pour le dialecte de la base
print(answer.rows[:5], answer.confidence.tier)
```

La spécification complète est dans [docs/architecture.md](../../docs/architecture.md).
