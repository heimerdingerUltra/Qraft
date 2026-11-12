# qraft-server

Passerelle HTTP et MCP du moteur Qraft. Aucune logique métier ici : chaque route délègue au moteur.

| Route | Rôle |
| --- | --- |
| `GET /v1/health` | État, version du graphe |
| `POST /v1/questions` | Pose une question ; `previous_id` pour une suite |
| `GET /v1/questions/stream?q=…` | Même chose en flux SSE : `interpretation`, `sql`, `rows`, `chart`, `confidence`, `done` |
| `POST /v1/questions/{id}/feedback` | Retour utilisateur (+1 / -1, commentaire) |
| `GET /v1/results/{id}?format=arrow` | Résultat en JSON ou en Arrow IPC |
| `GET /v1/knowledge/search?q=…` | Recherche dans le vocabulaire (métriques, dimensions, valeurs) |
| `GET /v1/knowledge/metrics` | Métriques certifiées |

```bash
qraft serve --db "duckdb:///phase0/demo/ventes.duckdb" --semantic examples/ventes.semantic.yaml
qraft-mcp --db "duckdb:///phase0/demo/ventes.duckdb" --semantic examples/ventes.semantic.yaml
```

L'identité vient des en-têtes `X-Qraft-User` et `X-Qraft-Attributes` (JSON) **uniquement** si `trust_identity_headers=True`, c'est-à-dire derrière une passerelle qui authentifie (OIDC) et qui écrase ces en-têtes.
