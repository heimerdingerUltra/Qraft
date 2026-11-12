# État d'implémentation

Ce document met en face la spécification ([architecture.md](architecture.md)) et ce que contient le dépôt. Il distingue ce qui est construit et testé, ce qui est écrit mais non exécuté ici (il faut une clé d'API, un GPU ou une vraie base), et ce qui reste à faire.

Mesure de référence, sur le jeu or de démonstration (14 questions, base DuckDB générée) : le moteur répond juste à 11 des 12 questions répondables, s'abstient sur les 2 questions sans réponse et ne donne **aucune réponse fausse**, sans aucun appel de modèle (`qraft-eval baseline … --generator engine`). La seule question manquée est une négation, que les règles ne couvrent pas par conception.

## Paquets

| Paquet | Rôle | Tests |
| --- | --- | --- |
| `packages/core` (`qraft`) | Moteur : QIR, graphe de connaissance, compilateur, parseur, vérification, confiance, exécution, CLI | oui |
| `packages/server` (`qraft_server`) | API REST, flux SSE, export Arrow, serveur MCP | oui |
| `packages/qraft-eval` | Harnais d'évaluation, baseline Claude, évaluation du moteur | oui |
| `packages/forge` (`qraft_forge`) | Forge : génération de données, vérification, jeu d'entraînement, récompense, entraînement | oui (hors entraînement) |

## Correspondance avec la spécification

| Section de la spec | État | Où |
| --- | --- | --- |
| QIR en trois niveaux | Construit : QIR typé (msgspec), plan logique implicite dans le compilateur, AST SQLGlot | `qir.py`, `compiler.py` |
| Passes de compilation | Construit : résolution, métriques composées, filtres intrinsèques, règles globales, jointures (Steiner approché, ambiguïtés signalées), pièges de grain (semi-jointures EXISTS, CTE pré-agrégées par grain), intelligence temporelle (grains, N-1, période précédente), ratios, politiques, abaissement dialectal, SQL régénéré depuis l'AST | `compiler.py`, `graph.py` |
| Explication déterministe | Construit | `explain.py` |
| Mode SQL libre | Construit : contrôlé par l'AST, interdit quand des filtres de lignes s'appliquent | `compiler.py` |
| Graphe de connaissance | Construit : catalogue, profilage, clés étrangères inférées (y compris sans clés déclarées), couche sémantique YAML, lexique, pack versionné | `catalog.py`, `semantic.py`, `graph.py` |
| Import dbt et conflits | Construit : manifest dbt, MetricFlow (mesures, métriques simples et ratios), fusion avec conflits | `importers.py` |
| Versionnement bitemporel | Construit pour les métriques : versions datées (validité métier) et date d'enregistrement (temps de connaissance) ; chaque période d'une comparaison prend sa propre définition | `semantic.py`, `compiler.py` |
| Studio de curation (interface) | À faire : l'API expose le vocabulaire et les conflits d'import ; l'écran des stewards n'est pas écrit | — |
| Parseur à règles (chemin C1) | Construit : périodes, classements, valeurs, ratios, suites de conversation, couverture mesurée | `parser.py` |
| Professeur Claude | Écrit et testé avec un faux client : QIR contraint par schéma JSON, cache de prompt, repli serveur. Non exécuté sans clé d'API | `models.py` |
| Élève local (vLLM) | Écrit et testé avec un faux client : décodage contraint par le schéma QIR. Non exécuté sans serveur vLLM | `models.py` |
| Routage | Construit : cache, règles, modèle, abstention ; routeur appris optionnel (régression logistique en Python pur, entraînée sur des questions étiquetées, au lieu de LightGBM) | `engine.py`, `router.py` |
| Bases distinctives | Construit : génération de petites bases DuckDB, contre-exemple montré à l'utilisateur | `verify.py`, `engine.py` |
| Confiance et calibration | Construit : signaux, score logistique, paliers, calibrateur isotone, ECE | `confidence.py` |
| Contrôles de vraisemblance | Construit : vide, valeurs nulles, troncature, double comptage, fraîcheur | `verify.py` |
| Exécution gouvernée | Construit : SQLAlchemy universel, concurrence par source, lecture seule ; lecture Arrow native (DuckDB, ConnectorX pour PostgreSQL, MySQL, SQL Server, Oracle, SQLite) avec plafond poussé dans la requête. Les pilotes ADBC génériques restent à brancher | `execute.py` |
| Identité propagée | Partiel : identité et filtres de lignes côté compilateur. L'échange OAuth ou l'impersonation côté base reste à faire | `policy.py` |
| Fédération multi-sources | Construit (v0) : tables lues dans leur base, jointure dans DuckDB en mémoire, plafond de lignes | `federation.py` |
| Analyse de causes | Construit : décomposition d'une variation par dimension, contributions exactes pour les métriques additives, effets mix et taux pour les ratios, récursion dans le premier segment, récit en français. Les moyennes (AVG) ne sont pas décomposées correctement | `insights.py` |
| Anomalies et moniteurs | Construit : médiane/MAD glissante (sans saisonnalité), moniteurs à seuil ou à anomalie, alertes console et webhook. La planification et l'analyse de causes jointe aux alertes restent à faire | `insights.py` |
| Agent analytique | Écrit et testé avec un faux client : boucle d'outils Claude (recherche, sous-question, sondage plafonné, transformation DuckDB, analyse de causes, graphique), budgets d'étapes, de jetons et d'appels à la base, données restreintes jamais envoyées. Non exécuté sans clé d'API | `agent.py` |
| Visualisation automatique | Construit : règles déterministes, spécification Vega-Lite | `viz.py` |
| SDK, CLI | Construit : `Engine`, `Answer.follow_up`, `qraft index / ask / explain / shell / serve` | `engine.py`, `cli.py` |
| API REST/SSE, export Arrow, MCP | Construit | `packages/server` |
| Interface web | Construit (première version) : conversation avec suites, interprétation, confiance, SQL, tableau, graphique Vega-Lite, retours ; servie par la passerelle sur `/` | `packages/server/.../static/index.html` |
| Bot Slack | Construit : Events API, signature vérifiée, réponse en fil, traitement en tâche de fond. Teams reste à faire | `packages/server/.../slack.py` |
| Boucle d'apprentissage | Partiel : journal d'événements (réponses, retours). Curation et réentraînement nocturne : outillés par la Forge, pas encore orchestrés | `engine.py`, `packages/forge` |
| Forge | Construit : échantillonnage d'intentions en 9 motifs avec rééquilibrage de couverture, vérification par exécution, verbalisation par gabarits, aller-retour, export SFT sans fuite, récompense d'exécution compatible TRL. Sur la démo : 300 intentions donnent 896 questions vérifiées en quelques secondes. Limites : diversité de langue limitée aux gabarits ; les paraphrases Claude (API Batch) sont préparées mais pas soumises ; script SFT LoRA + GRPO écrit, non exécuté (pas de GPU) | `packages/forge` |
| Infrastructure | Construit : Dockerfile, docker-compose de démo, CI GitHub Actions, chart Helm (déploiement, service, autoscaling, couche sémantique en ConfigMap ; non passé à `helm lint` ici, faute de binaire) | `deploy/`, `.github/` |
| Observabilité OpenTelemetry | Construit : un span par question (route, statut, confiance, durées par étape) ; l'export se configure avec le SDK OpenTelemetry | `telemetry.py` |

## Écarts assumés par rapport à la spec

- **Starlette + uvicorn** plutôt que Litestar + Granian : moins de dépendances, et la latence est dominée par le modèle et la base.
- **Pack de connaissance en JSON** (msgspec) plutôt qu'en fichier DuckDB : suffisant tant que le graphe tient en mémoire.
- **Planification des jointures en Python pur** : graphe petit, déterminisme maîtrisé ; rustworkx n'apporte rien à cette taille.
- **SDK MCP 2.x** (`MCPServer`) pris en charge, avec repli sur l'API 1.x (`FastMCP`).

## Pour aller plus loin

1. Lancer la baseline Claude sur un vrai domaine pilote (phase 0, voir [phase0/PHASE0.md](../phase0/PHASE0.md)).
2. Brancher ADBC et ConnectorX dans `execute.py`, puis l'identité propagée par base.
3. Exécuter la Forge et l'entraînement sur GPU, puis brancher l'élève via `LocalModel`.
4. Écrire le studio de curation et le bot Teams.
