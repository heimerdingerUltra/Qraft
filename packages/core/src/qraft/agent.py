"""Agent analytique : Claude mène une analyse en plusieurs étapes avec les outils du moteur.

Boucle d'outils manuelle sur le SDK officiel anthropic. L'agent n'a jamais plus de droits
qu'une question simple : chaque outil passe par le moteur (compilation, politiques,
vérification) ou par une lecture plafonnée et vérifiée. Aucune exécution de code arbitraire :
les calculs sur les résultats se font en SQL DuckDB, en mémoire, sans accès aux fichiers.

Garde-fous : budget (étapes, jetons cumulés, appels à la base), trace de chaque étape,
données des tables restreintes jamais transmises au modèle cloud.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlglot import exp

from .insights import QueryBudgetExceeded, explain_change
from .models import _nullable, _obj
from .policy import PolicyViolation, UnsafeSQL, check_columns, check_read_only
from .qir import Filter, TimeScope
from .text import STOPWORDS, phrase, tokens
from .viz import suggest_chart

if TYPE_CHECKING:
    from .engine import Engine
    from .graph import KnowledgeGraph

MODEL = "claude-opus-5-5"
PROBE_LIMIT = 50  # lignes au plus pour une requête de sondage
PREVIEW_ROWS = 20  # lignes renvoyées au modèle pour chaque résultat
MAX_STORED_ROWS = 10_000  # lignes conservées par table de l'espace de travail

SYSTEM = """Tu es l'agent analytique de Qraft. Tu mènes une analyse en plusieurs étapes en appelant les \
outils du moteur ; tu n'as aucun autre accès aux données.

Méthode :
- Si un terme de la demande n'est pas clairement une métrique ou une dimension du glossaire, cherche-le \
d'abord avec search_knowledge.
- Pose des sous-questions simples et précises avec ask (une métrique, des dimensions, une période explicite) : \
c'est le chemin certifié, à préférer à tout autre.
- probe sert seulement à explorer (valeurs distinctes, distribution d'une colonne) : lecture plafonnée à \
50 lignes.
- transform combine les résultats déjà obtenus (tables r1, r2…) en SQL DuckDB, sans accès à la base : \
utilise-le pour tout calcul (écarts, parts, classements), jamais le calcul de tête.
- explain_change explique la variation d'une métrique entre deux périodes (contribution de chaque segment).
- chart propose un graphique pour un résultat.
- Lance en parallèle les appels indépendants les uns des autres.
- Si un outil renvoie une erreur, corrige l'appel ou change d'approche.

Rapport final :
- En français, concis et structuré.
- Chaque chiffre vient d'un résultat d'outil et cite sa source entre crochets, par exemple [r2]. \
N'invente aucun chiffre.
- Signale les résultats à confirmer, les hypothèses d'interprétation et les limites des données.
- Si la demande ne peut pas être satisfaite avec ces données, dis-le simplement."""

LAST_STEP_NOTICE = ("Budget presque épuisé : rédige maintenant ton rapport final avec les résultats obtenus, "
                    "sans nouvel appel d'outil.")


@dataclass
class Budget:
    """Plafonds d'une analyse : appels au modèle, jetons cumulés, requêtes envoyées à la base."""

    max_steps: int = 12
    max_tokens: int = 400_000  # entrée (cache compris) et sortie, cumulées sur toute l'analyse
    max_db_calls: int = 40


@dataclass
class TraceStep:
    index: int
    kind: Literal["modele", "outil"]
    name: str
    status: str  # outil : ok | erreur ; modèle : stop_reason
    summary: str
    input: dict[str, Any] | None = None
    tokens: int = 0
    db_calls: int = 0
    elapsed_ms: float = 0.0


@dataclass
class SubAnswer:
    """Un résultat intermédiaire, enregistré comme table DuckDB et citable sous sa référence."""

    ref: str  # r1, r2… : nom de table pour transform et clé de citation dans le rapport
    tool: str
    request: str  # question, SQL ou description de l'analyse
    columns: list[str]
    rows: list[tuple[Any, ...]]
    status: str = "ok"
    interpretation: str = ""
    confidence: float | None = None
    tier: str | None = None
    sql: str | None = None
    n_dims: int | None = None


Status = Literal["termine", "refus", "tronque", "budget_epuise", "erreur"]


@dataclass
class AgentReport:
    request: str
    status: Status
    report: str  # texte final du modèle (ou dernier texte produit si l'analyse s'est arrêtée avant)
    sub_answers: list[SubAnswer] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    steps: int = 0
    tokens: int = 0
    db_calls: int = 0
    charts: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None

    @property
    def citations(self) -> list[SubAnswer]:
        """Sous-réponses citées dans le rapport final."""
        return [s for s in self.sub_answers if f"[{s.ref}]" in self.report]


class ToolError(RuntimeError):
    """Échec d'un outil, renvoyé au modèle comme tool_result en erreur."""


# --- Définition des outils ------------------------------------------------------------------


def tool_definitions(graph: KnowledgeGraph) -> list[dict[str, Any]]:
    """Outils stricts de l'agent. Liste stable et triée tant que le graphe ne change pas (cache)."""
    metrics, dimensions = sorted(graph.semantic.metrics), sorted(graph.semantic.dimensions)
    metric = {"type": "string", "enum": metrics} if metrics else {"type": "string"}
    dimension = {"type": "string", "enum": dimensions} if dimensions else {"type": "string"}
    period = _obj({"start": {"type": "string", "description": "Première date incluse, AAAA-MM-JJ."},
                   "end": {"type": "string", "description": "Date de fin exclue, AAAA-MM-JJ."}})

    def tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "description": description, "strict": True, "input_schema": _obj(properties)}

    return [
        tool("search_knowledge",
             "Cherche dans le graphe de connaissance les métriques, dimensions, tables et valeurs qui "
             "correspondent à un terme métier. À utiliser avant ask quand un mot de la demande est ambigu.",
             {"query": {"type": "string", "description": "Terme ou expression à chercher, en français."}}),
        tool("ask",
             "Pose une sous-question en français au moteur Qraft, qui l'interprète, la compile avec les règles "
             "métier et les politiques, l'exécute et la vérifie. Renvoie l'interprétation, les premières lignes, "
             "la confiance et la référence du résultat (r1, r2…).",
             {"question": {"type": "string", "description": "Question précise : métrique, dimensions, période."}}),
        tool("probe",
             "Requête de sondage en lecture seule sur la base, plafonnée à 50 lignes : valeurs distinctes ou "
             "distribution d'une colonne. Une seule instruction SELECT ; un LIMIT au-delà de 50 est ramené à 50.",
             {"sql": {"type": "string", "description": "Une instruction SELECT dans le dialecte de la base."}}),
        tool("transform",
             "SQL DuckDB (une instruction SELECT) sur les résultats déjà obtenus, disponibles comme tables r1, r2… "
             "Aucun accès à la base ni aux fichiers. Le résultat devient une nouvelle table.",
             {"sql": {"type": "string", "description": "SELECT qui ne lit que les tables r1, r2…"}}),
        tool("explain_change",
             "Explique la variation d'une métrique entre la période A et la période B : écart total, contribution "
             "de chaque segment des dimensions, principaux moteurs, puis détail dans le premier segment.",
             {"metric": metric, "period_a": period, "period_b": period,
              "dimensions": {"type": "array", "items": dimension,
                             "description": "Dimensions à examiner ; liste vide pour toutes."},
              "filters": {"type": "array", "description": "Restrictions d'égalité (liste vide si aucune).",
                          "items": _obj({"dimension": dimension, "value": {"type": "string"}})}}),
        tool("chart",
             "Propose un graphique (spécification Vega-Lite) pour un résultat déjà obtenu.",
             {"ref": {"type": "string", "description": "Référence du résultat, par exemple r1."},
              "dimensions": {**_nullable({"type": "integer"}),
                             "description": "Nombre de colonnes de dimension en tête ; null pour le déduire."}}),
    ]


# --- Espace de travail d'une analyse --------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _duck_type(values: list[Any]) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return "VARCHAR"
    if all(isinstance(v, bool) for v in present):
        return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in present):
        return "BIGINT"
    if all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in present):
        return "DOUBLE"
    if all(isinstance(v, datetime) for v in present):
        return "TIMESTAMP"
    if all(isinstance(v, date) and not isinstance(v, datetime) for v in present):
        return "DATE"
    return "VARCHAR"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


class _Workspace:
    """État d'une analyse : résultats enregistrés, base DuckDB en mémoire, compteurs, trace."""

    def __init__(self, engine: Engine, budget: Budget) -> None:
        self.engine = engine
        self.budget = budget
        self.results: dict[str, SubAnswer] = {}
        self.trace: list[TraceStep] = []
        self.charts: list[dict[str, Any]] = []
        self.db_calls = 0
        self._con: Any = None
        self._handlers = {"search_knowledge": self.search_knowledge, "ask": self.ask, "probe": self.probe,
                          "transform": self.transform, "explain_change": self.explain_change, "chart": self.chart}

    # --- Infrastructure ---

    @property
    def restricted(self) -> set[str]:
        return {t.lower() for t in self.engine.policy.restricted_tables}

    def _guard(self, tables: set[str]) -> None:
        hidden = {t.lower() for t in tables} & self.restricted
        if hidden:
            raise ToolError(f"données restreintes ({', '.join(sorted(hidden))}) : elles ne sont jamais transmises "
                            "au modèle cloud. Posez cette question en local.")

    def _reserve(self, n: int = 1) -> None:
        if self.db_calls + n > self.budget.max_db_calls:
            raise ToolError(f"budget d'appels à la base épuisé ({self.db_calls} / {self.budget.max_db_calls}) : "
                            "conclus avec les résultats déjà obtenus.")

    @property
    def con(self) -> Any:
        if self._con is None:
            import duckdb

            self._con = duckdb.connect(":memory:")
            # Ni fichiers, ni réseau, ni changement de configuration depuis une requête transform.
            self._con.execute("SET enable_external_access = false")
            self._con.execute("SET lock_configuration = true")
        return self._con

    def register(self, tool: str, request: str, columns: list[str], rows: list[tuple[Any, ...]],
                 **details: Any) -> SubAnswer:
        ref = f"r{len(self.results) + 1}"
        rows = rows[:MAX_STORED_ROWS]
        names: list[str] = []
        for column in columns:
            name, k = column, 2
            while name in names:
                name, k = f"{column}_{k}", k + 1
            names.append(name)
        types = [_duck_type([r[i] for r in rows]) for i in range(len(names))]
        self.con.execute(f"CREATE TABLE {_quote(ref)} (" + ", ".join(f"{_quote(n)} {t}" for n, t in zip(names, types))
                         + ")")
        if rows:
            def cell(v: Any, t: str) -> Any:
                if v is None:
                    return None
                if isinstance(v, Decimal):
                    return float(v)
                return str(v) if t == "VARCHAR" and not isinstance(v, str) else v

            self.con.executemany(f"INSERT INTO {_quote(ref)} VALUES (" + ", ".join("?" * len(names)) + ")",
                                 [tuple(cell(v, t) for v, t in zip(r, types)) for r in rows])
        sub = SubAnswer(ref=ref, tool=tool, request=request, columns=names, rows=list(rows), **details)
        self.results[ref] = sub
        return sub

    def _preview(self, sub: SubAnswer) -> dict[str, Any]:
        return {"ref": sub.ref, "colonnes": sub.columns, "lignes": [list(r) for r in sub.rows[:PREVIEW_ROWS]],
                "nb_lignes": len(sub.rows)}

    def execute(self, block: Any) -> dict[str, Any]:
        """Exécute un appel d'outil et renvoie le bloc tool_result correspondant."""
        started, db_before = time.perf_counter(), self.db_calls
        tool_input = dict(block.input or {})
        handler = self._handlers.get(block.name)
        try:
            if handler is None:
                raise ToolError(f"outil inconnu : {block.name}")
            content, is_error = _dump(handler(tool_input)), False
        except ToolError as error:
            content, is_error = str(error), True
        except Exception as error:  # noqa: BLE001 - l'échec d'un outil est rendu au modèle, jamais fatal
            content, is_error = f"{type(error).__name__} : {error}", True
        self.trace.append(TraceStep(
            index=len(self.trace) + 1, kind="outil", name=block.name, status="erreur" if is_error else "ok",
            summary=content[:300], input=tool_input, db_calls=self.db_calls - db_before,
            elapsed_ms=round(1000 * (time.perf_counter() - started), 1)))
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": content}
        if is_error:
            result["is_error"] = True
        return result

    # --- Outils ---

    def search_knowledge(self, args: dict[str, Any]) -> dict[str, Any]:
        graph = self.engine.graph
        query = str(args.get("query", ""))
        wanted = set(tokens(query)) - STOPWORDS
        canonical = phrase(query)
        priority = {"metric": 0, "dimension": 1, "entity": 2, "value": 3}
        scored = []
        for entry in graph.lexicon():
            if entry.kind == "value" and entry.ref.split(".")[0].lower() in self.restricted:
                continue
            words = set(entry.phrase.split())
            overlap = len(wanted & words)
            if overlap or (canonical and canonical in entry.phrase):
                scored.append((-overlap, priority[entry.kind], entry.phrase, entry))
        found: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str | None]] = set()
        for _, _, _, entry in sorted(scored, key=lambda s: s[:3]):
            key = (entry.kind, entry.ref, entry.value)
            if key in seen:
                continue
            seen.add(key)
            item: dict[str, Any] = {"type": entry.kind, "ref": entry.ref, "terme": entry.phrase}
            if entry.kind == "metric":
                m = graph.metric(entry.ref)
                item.update(libelle=m.label or m.id, unite=m.unit, description=m.description)
            elif entry.kind == "dimension":
                d = graph.semantic.dimensions[entry.ref]
                item.update(libelle=d.label or d.id, colonne=d.column)
            elif entry.kind == "entity":
                item["libelle"] = graph.label("entity", entry.ref)
            else:
                item["valeur"] = entry.value
            found.append(item)
            if len(found) >= 15:
                break
        if found:
            return {"resultats": found}
        return {"resultats": [], "message": "Aucun terme correspondant. Glossaire disponible ci-dessous.",
                "metriques": sorted(graph.semantic.metrics), "dimensions": sorted(graph.semantic.dimensions)}

    def ask(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question", "")).strip()
        if not question:
            raise ToolError("question vide")
        self._reserve(1)
        answer = self.engine.ask(question)
        if answer.sql is not None:
            self.db_calls += 1
        if answer.intent is not None and answer.sql is not None:
            self._guard(set(self.engine.compile(answer.intent).tables))
        if answer.status in ("abstention", "erreur"):
            raise ToolError(f"le moteur n'a pas pu répondre ({answer.status}) : {answer.message}")
        payload: dict[str, Any] = {"statut": answer.status, "interpretation": answer.interpretation}
        if answer.status == "clarification":
            payload.update(message=answer.message, alternatives=answer.alternatives)
            return payload
        confidence = answer.confidence
        sub = self.register("ask", question, answer.columns, answer.rows, status=answer.status,
                            interpretation=answer.interpretation, sql=answer.sql,
                            confidence=confidence.score if confidence else None,
                            tier=confidence.tier if confidence else None,
                            n_dims=len(answer.intent.dimensions) if answer.intent else None)
        payload.update(self._preview(sub))
        payload["confiance"] = None if confidence is None else {
            "score": confidence.score, "niveau": confidence.tier, "raisons": confidence.reasons}
        payload.update(hypotheses=answer.assumptions, regles=answer.rules, avertissements=answer.warnings)
        return payload

    def probe(self, args: dict[str, Any]) -> dict[str, Any]:
        sql = str(args.get("sql", ""))
        policy = self.engine.policy
        tree = check_read_only(sql, self.engine.graph.dialect)
        if not policy.allow_free_sql:
            raise PolicyViolation("les requêtes libres sont désactivées par la politique")
        if policy.row_filters:
            raise PolicyViolation("sondage interdit quand des filtres de lignes s'appliquent")
        check_columns(tree, policy)
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        self._guard({t.name for t in tree.find_all(exp.Table) if t.name.lower() not in ctes})
        tree = _cap_limit(tree, PROBE_LIMIT)
        dialect = self.engine.graph.catalog.dialect
        final_sql = tree.sql(dialect=dialect)
        if _limit_of(check_read_only(final_sql, dialect)) is None:
            raise UnsafeSQL("la requête de sondage doit porter un LIMIT d'au plus 50 lignes")
        self._reserve(1)
        self.db_calls += 1
        result = self.engine.source.query(final_sql, max_rows=PROBE_LIMIT)
        sub = self.register("probe", sql, result.columns, result.rows, sql=final_sql)
        return {**self._preview(sub), "sql_execute": final_sql}

    def transform(self, args: dict[str, Any]) -> dict[str, Any]:
        sql = str(args.get("sql", ""))
        tree = check_read_only(sql, "duckdb")
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            name = table.name.lower()
            if table.args.get("db") or table.args.get("catalog") or not name:
                raise UnsafeSQL(f"référence de table interdite : {table.sql(dialect='duckdb')}")
            if name not in ctes and name not in self.results:
                known = ", ".join(self.results) or "aucune"
                raise ToolError(f"table inconnue : {table.name} (tables disponibles : {known})")
        cursor = self.con.execute(tree.sql(dialect="duckdb"))
        columns = [d[0] for d in cursor.description]
        rows = [tuple(r) for r in cursor.fetchmany(MAX_STORED_ROWS)]
        sub = self.register("transform", sql, columns, rows, sql=sql)
        return self._preview(sub)

    def explain_change(self, args: dict[str, Any]) -> dict[str, Any]:
        def scope(key: str) -> TimeScope:
            period = args.get(key) or {}
            try:
                start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
            except (KeyError, TypeError, ValueError) as error:
                raise ToolError(f"{key} : dates attendues au format AAAA-MM-JJ ({error})") from error
            if end <= start:
                raise ToolError(f"{key} : la fin doit suivre le début")
            return TimeScope(start=start.isoformat(), end=end.isoformat())

        period_a, period_b = scope("period_a"), scope("period_b")
        filters = tuple(Filter(column=f["dimension"], op="=", value=f["value"]) for f in args.get("filters") or [])
        self._reserve(1)
        remaining = self.budget.max_db_calls - self.db_calls
        try:
            explanation = explain_change(self.engine, str(args.get("metric")), period_a, period_b,
                                         list(args.get("dimensions") or []) or None, filters=filters,
                                         max_queries=remaining)
        except QueryBudgetExceeded as error:
            self.db_calls += remaining
            raise ToolError(f"budget d'appels à la base insuffisant pour cette analyse ({error}) : "
                            "restreins les dimensions ou conclus.") from error
        self.db_calls += explanation.query_count
        self._guard(explanation.all_tables())
        rows = [(s.dimension, s.label, s.before, s.after, s.contribution, s.share)
                for b in explanation.breakdowns for s in b.segments]
        sub = self.register("explain_change", f"variation de {explanation.label}",
                            ["dimension", "segment", "avant", "apres", "contribution", "part_de_l_ecart"], rows,
                            interpretation=explanation.narrative.splitlines()[0], n_dims=2)
        return {"ref": sub.ref, **explanation.to_dict()}

    def chart(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("ref", ""))
        sub = self.results.get(ref)
        if sub is None:
            raise ToolError(f"résultat inconnu : {ref} (disponibles : {', '.join(self.results) or 'aucun'})")
        n_dims = args.get("dimensions")
        if n_dims is None:
            n_dims = sub.n_dims if sub.n_dims is not None else _leading_dimensions(sub)
        n_dims = max(0, min(int(n_dims), len(sub.columns) - 1))
        spec = suggest_chart(sub.columns, sub.rows, n_dims, title=sub.interpretation or None)
        if spec is None:
            return {"ref": ref, "graphique": None, "message": "un tableau suffit pour ce résultat"}
        self.charts.append({"ref": ref, "spec": spec})
        return {"ref": ref, "graphique": {"mark": spec["mark"], "encoding": spec["encoding"]},
                "message": "spécification Vega-Lite jointe au rapport"}


def _limit_of(tree: exp.Expression) -> int | None:
    limit = tree.args.get("limit")
    node = limit.args.get("expression") if limit is not None else None
    if isinstance(node, exp.Literal) and node.is_int:
        return int(node.this)
    return None


def _cap_limit(tree: exp.Expression, cap: int) -> exp.Expression:
    """Pose LIMIT cap si la requête n'a pas de LIMIT littéral au plus égal à cap."""
    current = _limit_of(tree)
    if current is not None and current <= cap:
        return tree
    if not hasattr(tree, "limit"):
        raise UnsafeSQL("impossible de plafonner cette requête")
    return tree.limit(cap)


def _leading_dimensions(sub: SubAnswer) -> int:
    count = 0
    for i in range(len(sub.columns)):
        values = [r[i] for r in sub.rows if r[i] is not None]
        if values and all(_is_number(v) for v in values):
            break
        count += 1
    return min(count, max(len(sub.columns) - 1, 0))


def _tokens(usage: Any) -> int:
    if usage is None:
        return 0
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(getattr(usage, k, 0) or 0) for k in keys)


def _text(content: Any) -> str:
    return "\n".join(b.text for b in content or [] if getattr(b, "type", None) == "text" and b.text).strip()


def _api_errors() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """(erreurs de configuration à remonter, erreurs d'appel qui arrêtent proprement l'analyse)."""
    try:
        import anthropic
    except ImportError:
        return (), ()
    return ((anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError),
            (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError))


# --- Agent ----------------------------------------------------------------------------------


class AnalystAgent:
    """Agent analytique : Claude (SDK anthropic) en boucle d'outils manuelle sur le moteur Qraft."""

    def __init__(self, engine: Engine, *, client: Any | None = None, model: str = MODEL, effort: str = "high",
                 max_tokens: int = 16000, budget: Budget | None = None, fallbacks: bool = True) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.engine = engine
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.budget = budget or Budget()
        self.fallbacks = fallbacks
        self.tools = tool_definitions(engine.graph)
        summary = engine.graph.summary_for_prompt()
        hidden = engine.policy.restricted_tables
        if hidden:  # données restreintes : leurs valeurs ne partent jamais vers le cloud
            summary = "\n".join(line for line in summary.splitlines()
                                if not any(line.startswith(f"- {t}(") for t in hidden))
        self.system = SYSTEM + "\n\nDomaine :\n" + summary

    def _create(self, messages: list[dict[str, Any]]) -> Any:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "tools": self.tools,
            "tool_choice": {"type": "auto"},
            "messages": messages,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},  # préfixe (outils, système, historique) mis en cache
        }
        if self.fallbacks:
            return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                    **request)
        return self.client.messages.create(**request)

    def run(self, request: str) -> AgentReport:
        """Mène l'analyse demandée et renvoie le rapport final, les sous-réponses et la trace."""
        workspace = _Workspace(self.engine, self.budget)
        budget = self.budget
        context = (f"Date du jour : {self.engine.parser.today.isoformat()}. Budget : {budget.max_steps} étapes, "
                   f"{budget.max_db_calls} requêtes à la base.")
        messages: list[dict[str, Any]] = [{"role": "user", "content": [
            {"type": "text", "text": request}, {"type": "text", "text": context}]}]
        fatal, recoverable = _api_errors()
        steps = tokens_used = 0
        status: Status = "budget_epuise"
        message: str | None = None
        last_text = ""
        while True:
            if steps >= budget.max_steps:
                message = f"budget d'étapes épuisé ({budget.max_steps})"
                break
            if tokens_used >= budget.max_tokens:
                message = f"budget de jetons épuisé ({tokens_used} / {budget.max_tokens})"
                break
            started = time.perf_counter()
            try:
                response = self._create(messages)
            except fatal:
                raise
            except recoverable as error:
                status, message = "erreur", f"appel au modèle impossible : {error}"
                break
            steps += 1
            used = _tokens(getattr(response, "usage", None))
            tokens_used += used
            content = response.content
            text = _text(content)
            last_text = text or last_text
            calls = [b for b in content if getattr(b, "type", None) == "tool_use"]
            workspace.trace.append(TraceStep(
                index=len(workspace.trace) + 1, kind="modele", name=self.model, status=str(response.stop_reason),
                summary=text[:300] or f"{len(calls)} appel(s) d'outil", tokens=used,
                elapsed_ms=round(1000 * (time.perf_counter() - started), 1)))
            # Le contenu intégral (réflexion comprise) repart dans l'historique, sans retouche.
            messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "tool_use":
                if not calls:
                    status, message = "erreur", "arrêt tool_use sans appel d'outil"
                    break
                if steps >= budget.max_steps or tokens_used >= budget.max_tokens:
                    message = ("budget épuisé avant l'exécution des derniers appels d'outil "
                               f"({steps} étapes, {tokens_used} jetons)")
                    break
                results: list[dict[str, Any]] = [workspace.execute(block) for block in calls]
                if steps == budget.max_steps - 1 or tokens_used >= 0.8 * budget.max_tokens:
                    results.append({"type": "text", "text": LAST_STEP_NOTICE})
                messages.append({"role": "user", "content": results})  # tous les résultats, un seul message
                continue
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "end_turn":
                status = "termine"
            elif response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details is not None else None
                status = "refus"
                message = "le modèle a décliné la demande" + (f" (catégorie : {category})" if category else "")
            elif response.stop_reason == "max_tokens":
                status, message = "tronque", f"réponse coupée à {self.max_tokens} jetons de sortie"
            else:
                status, message = "erreur", f"arrêt inattendu du modèle : {response.stop_reason}"
            break
        return AgentReport(
            request=request, status=status, report=last_text, sub_answers=list(workspace.results.values()),
            trace=workspace.trace, steps=steps, tokens=tokens_used, db_calls=workspace.db_calls,
            charts=workspace.charts, message=message)


def analyze(engine: Engine, request: str, **kwargs: Any) -> AgentReport:
    """Raccourci : AnalystAgent(engine, **kwargs).run(request)."""
    return AnalystAgent(engine, **kwargs).run(request)
