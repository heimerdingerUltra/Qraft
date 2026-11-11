# Phase 0 — Mesurer avant de construire

La phase 0 produit trois chiffres sur le domaine pilote. Sans eux, aucune phase suivante n'est justifiable :

1. **La baseline** : ce que fait un grand modèle seul, avec le schéma complet et rien d'autre.
2. **La couverture du QIR** : la part des vraies questions que le QIR v0 sait exprimer.
3. **L'écart de l'élève** : à quelle distance un petit modèle spécialisé arrive de la baseline.

## Gate 0

| Critère | Seuil | Outil |
| --- | --- | --- |
| Baseline mesurée sur le jeu or | — | `qraft-eval baseline` |
| Couverture QIR v0 | ≥ 85 % des questions répondables | `qraft-eval coverage` |
| Élève pilote | à moins de 10 points de la baseline | spike GPU (étape 8) |

Une gate manquée ne signifie pas l'arrêt du projet : on corrige la piste (étendre le QIR, changer de modèle de base) ou on l'abandonne, chiffres à l'appui.

## Prérequis

- **Un domaine pilote et sa base**, accessible depuis le poste avec un compte **sans aucun droit d'écriture**. Ce point est obligatoire : le harnais filtre les requêtes, mais ne remplace pas les droits de la base.
- **Un ou deux experts métier**, pour écrire et valider le jeu or.
- **Une clé d'API Claude** (`ANTHROPIC_API_KEY`), et l'accord de l'entreprise pour envoyer à Anthropic le schéma et les questions (voir « Données et confidentialité »).
- **Python 3.12 ou plus.**

## Installation (PowerShell)

Sous Windows, le venv doit vivre dans un chemin court : certains fichiers de SQLAlchemy dépassent sinon la limite de 260 caractères.

```powershell
python -m venv $env:USERPROFILE\.venvs\qraft
& $env:USERPROFILE\.venvs\qraft\Scripts\Activate.ps1
pip install -e "packages/qraft-eval[baseline,duckdb,dev]"
pip install "psycopg[binary]"   # ou le driver de ta base : snowflake-sqlalchemy, pyodbc, oracledb...
pytest packages/qraft-eval
```

## Étapes

1. **Tester le harnais sur la démo.**

   ```powershell
   qraft-eval demo-db
   $env:QRAFT_DB_URL = "duckdb:///phase0/demo/ventes.duckdb"
   qraft-eval validate phase0/goldsets/demo_ventes.yaml
   qraft-eval baseline phase0/goldsets/demo_ventes.yaml --dry-run
   ```

2. **Constituer le jeu or du domaine pilote** à partir de [`goldsets/TEMPLATE.yaml`](goldsets/TEMPLATE.yaml). Règles d'écriture :
   - Viser 200 questions : environ 40 % faciles, 40 % moyennes, 20 % difficiles, et 10 % sans réponse possible.
   - Partir de questions réellement posées : demandes à l'équipe data, tickets, messages, rapports existants. Pas de questions inventées par l'équipe technique.
   - Formuler chaque question comme un utilisateur métier la pose, jargon compris.
   - Faire valider chaque SQL de référence par un expert. La référence fixe le format attendu (colonnes, unité, granularité). Si ce format n'est pas évident, l'écrire dans la question : la comparaison est stricte.
   - Renseigner la difficulté, les tags (`comptage`, `période`, `classement`, `jointure`, `métrique-métier`, `piège-de-grain`, `négation`, `comparaison-temporelle`, `part`, `valeur`, `sans-réponse`) et, dans `notes`, la règle métier en jeu.
   - Ne jamais utiliser ces questions pour entraîner quoi que ce soit : le jeu or sert uniquement à mesurer.

3. **Valider le jeu or** : chaque référence doit s'exécuter et renvoyer au moins une ligne.

   ```powershell
   $env:QRAFT_DB_URL = "postgresql+psycopg://lecteur@hote/base"
   qraft-eval validate phase0/goldsets/pilote.yaml
   ```

4. **Relire ce qui partira chez Anthropic** : `qraft-eval schema` affiche le schéma exactement tel qu'il sera envoyé. Avec `--samples`, il ajoute les valeurs fréquentes des colonnes texte ; ne l'utiliser que si ces valeurs peuvent sortir.

5. **Estimer le coût** : `qraft-eval baseline phase0/goldsets/pilote.yaml --dry-run`.

6. **Lancer la baseline**, d'abord sur 5 questions pour vérifier le rapport, puis sur tout le jeu :

   ```powershell
   qraft-eval baseline phase0/goldsets/pilote.yaml --limit 5
   qraft-eval baseline phase0/goldsets/pilote.yaml
   ```

   Le rapport (`report.md`), le détail par question (`results.jsonl`) et la synthèse (`summary.json`) sont écrits dans `phase0/results/<domaine>-<date>/`.

7. **Annoter le QIR v0** : renseigner `qir_expressible` pour chaque question répondable (règles ci-dessous), puis `qraft-eval coverage phase0/goldsets/pilote.yaml`.

8. **Spike élève**, après la baseline (voir plus bas).

9. **Décider la gate 0** avec les trois chiffres.

## QIR v0 : ce qui est exprimable

Une question est exprimable en QIR v0 si elle se ramène à :

- une ou plusieurs **mesures** : SUM, COUNT, COUNT DISTINCT, AVG, MIN, MAX sur une colonne, une métrique du glossaire, ou le ratio de deux mesures ;
- zéro ou plusieurs **dimensions** : colonnes de regroupement, ou grain temporel (jour, semaine, mois, trimestre, année) ;
- des **filtres combinés par ET** : égalité, différence, comparaison, intervalle, liste de valeurs, contient, est vide ;
- une **période**, absolue ou relative, et éventuellement une **comparaison** (N-1, période précédente, évolution en %) ;
- un **classement** top ou bottom k sur une mesure.

Les jointures n'ont pas à figurer dans la question : le compilateur les calcule.

Ne sont pas exprimables en v0 :

- les négations et anti-jointures (« aucun », « jamais », « sans ») ;
- les sous-requêtes corrélées et les filtres sur agrégats imbriqués (« clients dont le CA dépasse la moyenne de leur région ») ;
- les fenêtres complexes (rang dans un groupe, cumul glissant non standard) ;
- la récursivité et les raisonnements en plusieurs étapes.

Quand `qir_expressible` vaut `false`, noter la raison dans `notes` : ces raisons guident l'extension du QIR.

## Spike élève (après la baseline)

Le harnais accepte tout générateur qui respecte le protocole `SqlGenerator` (`baseline.py`). Le spike branche un modèle local :

1. Choisir deux modèles de base de 7 à 8 milliards de paramètres, à poids ouverts, servis par vLLM sur un GPU de 24 à 80 Go.
2. Générer environ 5 000 paires question → SQL sur le schéma pilote, vérifiées par exécution (une Forge minimale).
3. Entraîner un adapter LoRA (une époque de SFT), puis le mesurer sur le jeu or avec ce harnais.
4. Comparer à la baseline : la gate demande au plus 10 points d'écart.

Ce spike n'est pas encore outillé dans ce dépôt : il vient après la baseline.

## Données et confidentialité

- **Envoyé à Anthropic** : les consignes, le schéma (noms de tables et de colonnes, types, clés) et les questions. Avec `--samples`, les valeurs fréquentes des colonnes texte en plus. Aucune ligne de résultat n'est envoyée.
- **Exécuté en local** : toutes les requêtes, sur ta base, avec ton compte en lecture seule.
- **Repli serveur** : activé par défaut. Si Claude Opus 5.5 décline une question, l'API la rejoue sur un autre modèle. Le rapport liste ces questions, car elles ne mesurent pas le modèle demandé. `--no-fallbacks` le désactive.
- **Git** : `phase0/results/` et les fichiers `*.duckdb` sont ignorés, car ils peuvent contenir des données réelles.

## Lire le rapport

| Statut | Signification |
| --- | --- |
| `correct` | Même résultat que la référence |
| `incorrect` | La requête s'exécute mais le résultat diffère |
| `erreur_sql` | La requête échoue sur la base |
| `sql_refuse` | La requête n'est pas une lecture sûre (écriture, fonction interdite, plusieurs instructions) |
| `abstention_a_tort` | Le modèle a jugé sans réponse une question répondable |
| `abstention_correcte` | Le modèle s'est abstenu sur une question sans réponse |
| `reponse_a_tort` | Le modèle a répondu à une question sans réponse |
| `refus` | Le modèle a décliné la demande |
| `erreur_api` | L'appel au modèle a échoué |
