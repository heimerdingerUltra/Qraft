"""Vérifications de la Forge : exécution de référence (étape 3) et aller-retour (étape 5).

- ExecutionChecker compile chaque intention puis l'exécute sur la source ; il rejette les
  erreurs, les résultats vides, tronqués ou dégénérés (une mesure entièrement nulle ou à zéro),
  les pièges de grain et les jointures ambiguës. Le résultat retenu sert de référence.
- RoundTrip re-interprète chaque question générée sans voir l'intention d'origine (parseur
  à règles, ou tout modèle injecté) et ne garde la paire que si l'interprétation donne le
  même résultat d'exécution que la référence. C'est le filtre qualité central.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from qraft.compiler import Compiler
from qraft.execute import Source, SourceError
from qraft.graph import KnowledgeGraph
from qraft.parser import RuleParser
from qraft.qir import Intent, Measure
from qraft.verify import same_results

# Une interprétation : question -> intention, ou None si l'interpréteur s'abstient.
Interpreter = Callable[[str], Intent | None]


class Rejected(Exception):
    """L'intention ne donne pas de référence exploitable."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason} : {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Reference:
    """Une intention vérifiée et son résultat d'exécution."""

    intent: Intent
    sql: str
    columns: list[str]
    rows: list[tuple[Any, ...]]
    tables: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def ordered(self) -> bool:
        """Un classement rend l'ordre des lignes significatif."""
        return self.intent.ranking is not None


def _nullish(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and value == 0)


def results_match(reference: Reference, other: Reference) -> bool:
    """Même résultat d'exécution. Les colonnes de même nom sont réalignées (l'ordre des
    dimensions ou des mesures d'une interprétation n'a pas d'importance)."""
    if len(reference.columns) != len(other.columns):
        return False
    rows: Sequence[tuple[Any, ...]] = other.rows
    if other.columns != reference.columns and sorted(other.columns) == sorted(reference.columns) \
            and len(set(other.columns)) == len(other.columns):
        index = [other.columns.index(c) for c in reference.columns]
        rows = [tuple(r[i] for i in index) for r in other.rows]
    return same_results(reference.rows, rows, ordered=reference.ordered)


def fanout_safe(intent: Intent, graph: KnowledgeGraph) -> bool:
    """Un piège de grain est sans danger si toutes les mesures sont des comptages d'entités
    à clé simple : le compilateur les réécrit en COUNT(DISTINCT clé)."""
    measures: list[Measure] = list(intent.measures) + [m for r in intent.ratios for m in (r.numerator, r.denominator)]

    def safe(m: Measure) -> bool:
        if m.metric or m.agg != "count" or not m.column or "." in m.column:
            return False
        tbl = graph.catalog.table(m.column)
        return tbl is not None and len(tbl.primary_key) == 1

    return bool(measures) and all(safe(m) for m in measures)


class ExecutionChecker:
    """Compile et exécute des intentions ; met en cache références et rejets par empreinte."""

    def __init__(self, graph: KnowledgeGraph, source: Source, *, compiler: Compiler | None = None,
                 max_rows: int | None = None) -> None:
        self.graph = graph
        self.source = source
        self.compiler = compiler or Compiler(graph)
        self.max_rows = max_rows or self.compiler.policy.max_rows
        self.rejections: Counter[str] = Counter()
        self._cache: dict[str, Reference | Rejected] = {}

    def run(self, intent: Intent) -> Reference:
        """Référence de l'intention ; lève Rejected si elle n'est pas exploitable."""
        key = intent.fingerprint()
        cached = self._cache.get(key)
        if cached is None:
            try:
                cached = self._run(intent)
            except Rejected as rejection:
                cached = rejection
            self._cache[key] = cached
        if isinstance(cached, Rejected):
            raise Rejected(cached.reason, cached.detail)
        return cached

    def check(self, intent: Intent) -> Reference | None:
        """Comme run, mais compte le motif de rejet et retourne None."""
        try:
            return self.run(intent)
        except Rejected as rejection:
            self.rejections[rejection.reason] += 1
            return None

    def _run(self, intent: Intent) -> Reference:
        if not intent.answerable:
            raise Rejected("non_repondable")
        try:
            compiled = self.compiler.compile(intent)
        except Exception as error:  # noqa: BLE001 - toute intention (même produite par un modèle) doit être jugée
            raise Rejected("compilation", str(error)) from error
        if compiled.warnings:
            raise Rejected("avertissement", "; ".join(compiled.warnings))
        if compiled.signals.get("ambiguous_join"):
            raise Rejected("jointure_ambigue")
        if compiled.signals.get("fanout") and not fanout_safe(intent, self.graph):
            raise Rejected("piege_de_grain")
        try:
            result = self.source.query(compiled.sql, max_rows=self.max_rows)
        except SourceError as error:
            raise Rejected("execution", str(error)) from error
        if result.truncated:
            raise Rejected("trop_de_lignes")
        if not result.rows:
            raise Rejected("vide")
        dims = int(compiled.signals.get("dims", 0))
        for i in range(dims, len(result.columns)):
            if all(_nullish(r[i]) for r in result.rows):
                raise Rejected("degenere", f"mesure entièrement nulle : {result.columns[i]}")
        if intent.ratios and not intent.measures:
            whole = 100.0 if all(r.percent for r in intent.ratios) else 1.0
            shares = [r[i] for r in result.rows for i in range(dims, len(result.columns))]
            if all(v is None or float(v) in (0.0, whole) for v in shares):
                raise Rejected("degenere", "part toujours égale à 0 ou 100 %")
        limit = intent.ranking.limit if intent.ranking else None
        if dims and len(result.rows) == 1 and not limit:
            raise Rejected("degenere", "un regroupement qui ne donne qu'une ligne")
        if limit and limit > 1 and len(result.rows) < limit:
            raise Rejected("classement_trivial", f"{len(result.rows)} groupes pour un top {limit}")
        return Reference(intent, compiled.sql, list(result.columns), list(result.rows), list(compiled.tables))


# --- Aller-retour --------------------------------------------------------------------------------


def rule_interpreter(graph: KnowledgeGraph, *, min_coverage: float = 0.75, today: date | None = None) -> Interpreter:
    """Interpréteur par défaut : le parseur à règles, avec le même seuil de couverture que le moteur."""
    parser = RuleParser(graph, today=today)

    def interpret(question: str) -> Intent | None:
        parsed = parser.parse(question)
        if not parsed.handled or parsed.coverage < min_coverage:
            return None
        return parsed.intent

    return interpret


def model_interpreter(model: Any) -> Interpreter:
    """Interpréteur à partir d'un modèle (ModelPort de qraft.models : propose(question, n=1))."""

    def interpret(question: str) -> Intent | None:
        proposals = [i for i in model.propose(question, n=1) if i.answerable]
        return proposals[0] if proposals else None

    return interpret


@dataclass
class RoundTripOutcome:
    kept: bool
    reason: str
    reparsed: Intent | None = None


class RoundTrip:
    """Ne garde une question que si son interprétation indépendante donne le résultat de référence."""

    def __init__(self, checker: ExecutionChecker, interpreter: Interpreter) -> None:
        self.checker = checker
        self.interpreter = interpreter
        self.outcomes: Counter[str] = Counter()

    def verify(self, reference: Reference, question: str) -> RoundTripOutcome:
        outcome = self._verify(reference, question)
        self.outcomes[outcome.reason] += 1
        return outcome

    def _verify(self, reference: Reference, question: str) -> RoundTripOutcome:
        try:
            reparsed = self.interpreter(question)
        except Exception:  # noqa: BLE001 - un interpréteur défaillant vaut une abstention
            reparsed = None
        if reparsed is None:
            return RoundTripOutcome(False, "non_interpretee")
        if reparsed.fingerprint() == reference.intent.fingerprint():
            return RoundTripOutcome(True, "identique", reparsed)
        try:
            other = self.checker.run(reparsed)
        except Rejected:
            return RoundTripOutcome(False, "reinterpretation_rejetee", reparsed)
        if results_match(reference, other):
            return RoundTripOutcome(True, "meme_resultat", reparsed)
        return RoundTripOutcome(False, "resultat_different", reparsed)
