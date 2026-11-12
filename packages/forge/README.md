# qraft-forge (Forge)

La Forge compile une base en jeu d'entraînement pour un modèle spécialisé : des paires question française -> intention QIR, toutes vérifiées par exécution. Elle suit la section « Forge » de [docs/architecture.md](../../docs/architecture.md).

| Étape | Module |
| --- | --- |
| Échantillonnage d'intentions (graine, pondération par couverture) | `sampler.py` |
| Compilation et exécution de référence, rejets (vide, dégénéré, piège de grain...) | `checks.py` |
| Verbalisation par gabarits ; paraphrase par Claude (API Message Batches) | `verbalize.py` |
| Vérification aller-retour (parseur à règles ou modèle injecté) | `checks.py` |
| Export JSONL SFT « chat », découpage train/validation par intention | `dataset.py` |
| Récompense d'exécution pour GRPO (signature TRL) | `reward.py` |
| Entraînement SFT LoRA puis GRPO (GPU) | `train.py` |

```bash
qraft-forge generate --db duckdb:///phase0/demo/ventes.duckdb --semantic examples/ventes.semantic.yaml \
    --out forge --n 2000 --seed 42
qraft-forge stats forge
```

`generate` écrit `train.jsonl`, `validation.jsonl` et `manifest.json` (entonnoir : tirages, rejets par motif, taux d'aller-retour, couverture). Avec `--paraphrase-requests`, il écrit aussi le lot de paraphrases Claude prêt à soumettre, sans l'envoyer.

Chaque ligne JSONL contient le prompt système du modèle servi (`models.INSTRUCTIONS` + résumé du domaine), la question, et le JSON complet de l'intention, conforme au décodage contraint par `qir_json_schema`.

Entraînement (machine GPU) : `pip install 'qraft-forge[train]'` puis `python -m qraft_forge.train --help`.
