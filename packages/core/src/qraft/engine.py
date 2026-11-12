"""Façade du moteur : question -> interprétation -> SQL -> exécution -> vérification -> réponse.

Routage (sans appel de modèle sur le chemin critique) :
cache d'interprétation, puis parseur à règles si sa couverture suffit, puis modèle
(élève local ou professeur), sinon abstention argumentée. Plusieurs candidats qui
divergent sur une base distinctive deviennent une question de clarification.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .catalog import infer_foreign_keys, introspect, profile
from .compiler import Compiled, Compiler
from .confidence import Calibrator, Confidence, estimate
from .execute import Result, Source, SourceError
from .explain import Explainer
from .graph import CompileError, KnowledgeGraph, KnowledgePack
from .models import ModelPort
from .parser import RuleParser
from .policy import Identity, Policy, PolicyViolation, UnsafeSQL
from .qir import Intent
from .semantic import load_semantic
from .text import phrase
from .verify import DistinguishingDB, plausibility
from .viz import suggest_chart

MIN_RULE_COVERAGE = 0.75


class EventSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


class JsonlEventSink:
    """Journal d'événements immuable (une ligne JSON par événement) pour la boucle d'apprentissage."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@dataclass
class Answer:
    question: str
    status: str  # ok | a_confirmer | clarification | abstention | erreur
    interpretation: str = ""
    intent: Intent | None = None
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    confidence: Confidence | None = None
    route: str = ""
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    chart: dict | None = None
    message: str | None = None
    alternatives: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    _engine: Engine | None = field(default=None, repr=False, compare=False)

    def follow_up(self, question: str) -> Answer:
        if self._engine is None:
            raise RuntimeError("réponse détachée de son moteur")
        return self._engine.ask(question, previous=self)

    def to_arrow(self) -> Any:
        return Result(self.columns, self.rows).to_arrow()

    def to_dict(self) -> dict[str, Any]:
        def clean(v: Any) -> Any:
            return v.isoformat() if hasattr(v, "isoformat") else (float(v) if type(v).__name__ == "Decimal" else v)

        return {
            "id": self.id, "question": self.question, "status": self.status,
            "interpretation": self.interpretation, "intent": self.intent.to_dict() if self.intent else None,
            "sql": self.sql, "columns": self.columns, "rows": [[clean(v) for v in r] for r in self.rows],
            "confidence": None if self.confidence is None else {
                "score": self.confidence.score, "tier": self.confidence.tier, "reasons": self.confidence.reasons},
            "route": self.route, "assumptions": self.assumptions, "warnings": self.warnings, "rules": self.rules,
            "chart": self.chart, "message": self.message, "alternatives": self.alternatives,
            "timings_ms": self.timings_ms,
        }


def build_graph(source: Source, *, semantic: str | Path | None = None, do_profile: bool = True,
                infer: bool = True, schema: str | None = None) -> KnowledgeGraph:
    """Indexation : introspection, profilage, clés inférées, couche sémantique."""
    catalog = introspect(source.engine, schema=schema)
    if do_profile:
        profile(source.engine, catalog)
    if infer:
        infer_foreign_keys(source.engine, catalog)
    return KnowledgeGraph(catalog, load_semantic(semantic) if semantic else None)


class Engine:
    def __init__(
        self,
        source: Source,
        graph: KnowledgeGraph,
        *,
        model: ModelPort | None = None,
        policy: Policy | None = None,
        identity: Identity | None = None,
        today: date | None = None,
        min_confidence: float = 0.6,
        samples: int = 1,
        events: EventSink | None = None,
        calibrator: Calibrator | None = None,
        cache_size: int = 512,
    ) -> None:
        self.source = source
        self.graph = graph
        self.model = model
        self.policy = policy or Policy()
        self.identity = identity or Identity()
        self.parser = RuleParser(graph, today=today)
        self.explainer = Explainer(graph)
        self.min_confidence = min_confidence
        self.samples = samples
        self.events = events
        self.calibrator = calibrator
        self._cache: OrderedDict[tuple[str, ...], tuple[Intent, str, float]] = OrderedDict()
        self._cache_size = cache_size

    @classmethod
    def from_url(cls, url: str, *, semantic: str | Path | None = None, pack: str | Path | None = None,
                 rebuild: bool = False, schema: str | None = None, **kwargs: Any) -> Engine:
        source = Source(url)
        if pack and Path(pack).exists() and not rebuild:
            graph = KnowledgeGraph.from_pack(KnowledgePack.load(pack))
            if semantic:  # la couche sémantique peut évoluer sans réindexer la base
                graph = KnowledgeGraph(graph.catalog, load_semantic(semantic))
        else:
            graph = build_graph(source, semantic=semantic, schema=schema)
            if pack:
                graph.to_pack().save(pack)
        return cls(source, graph, **kwargs)

    # --- Étapes ------------------------------------------------------------------------------

    def compiler(self, identity: Identity | None = None) -> Compiler:
        return Compiler(self.graph, policy=self.policy, identity=identity or self.identity)

    def compile(self, intent: Intent, identity: Identity | None = None) -> Compiled:
        return self.compiler(identity).compile(intent)

    def _cache_key(self, question: str, previous: Answer | None) -> tuple[str, ...]:
        prev = previous.intent.fingerprint() if previous and previous.intent else ""
        return phrase(question), self.graph.version, prev

    def _record(self, answer: Answer, identity: Identity) -> None:
        if self.events is not None:
            self.events.record({"type": "answer", "at": time.time(), "user": identity.user, **answer.to_dict()})

    def feedback(self, answer_id: str, *, rating: int, comment: str | None = None,
                 corrected_intent: Intent | None = None, identity: Identity | None = None) -> None:
        """Retour utilisateur (+1 / -1, correction d'interprétation) pour la boucle d'apprentissage."""
        if self.events is not None:
            self.events.record({
                "type": "feedback", "at": time.time(), "answer_id": answer_id, "rating": rating,
                "comment": comment, "user": (identity or self.identity).user,
                "corrected_intent": corrected_intent.to_dict() if corrected_intent else None,
            })

    # --- Question ----------------------------------------------------------------------------

    def ask(self, question: str, *, previous: Answer | None = None, execute: bool = True,
            identity: Identity | None = None) -> Answer:
        identity = identity or self.identity
        timings: dict[str, float] = {}
        started = time.perf_counter()
        answer = Answer(question=question, status="abstention", _engine=self)

        key = self._cache_key(question, previous)
        parse = self.parser.parse(question, previous.intent if previous else None)
        candidates: list[Intent] = []
        coverage = parse.coverage
        if key in self._cache:
            intent, route, coverage = self._cache[key]
            self._cache.move_to_end(key)
            candidates, answer.route = [intent], "cache"
        elif parse.handled and parse.coverage >= MIN_RULE_COVERAGE:
            candidates, answer.route = [parse.intent], "regles"
        elif self.model is not None:
            t = time.perf_counter()
            candidates = [c for c in self.model.propose(question, previous=previous.intent if previous else None,
                                                        n=self.samples) if c.answerable]
            timings["modele"] = round(1000 * (time.perf_counter() - t), 1)
            answer.route = "modele"
            coverage = 1.0
        timings["interpretation"] = round(1000 * (time.perf_counter() - started), 1)

        if not candidates:
            unknown = f" Mots non reconnus : {', '.join(parse.unmatched)}." if parse.unmatched else ""
            answer.message = ("Je ne peux pas répondre de façon fiable à cette question avec les données et le "
                              f"vocabulaire connus.{unknown} Reformulez, ou ajoutez la définition au glossaire.")
            if parse.reason:
                answer.warnings.append(parse.reason)
            answer.timings_ms = timings
            self._record(answer, identity)
            return answer

        compiler = self.compiler(identity)
        compiled: list[tuple[Intent, Compiled]] = []
        errors = []
        for intent in candidates:
            try:
                compiled.append((intent, compiler.compile(intent)))
            except (CompileError, PolicyViolation, UnsafeSQL) as error:
                errors.append(str(error))
        timings["compilation"] = round(1000 * (time.perf_counter() - started) - sum(timings.values()), 1)
        if not compiled:
            answer.status, answer.message = "erreur", "Interprétation impossible à compiler : " + " ; ".join(errors)
            answer.timings_ms = timings
            self._record(answer, identity)
            return answer

        intent, plan = compiled[0]
        if any(i.escape_sql for i, _ in compiled[:1]):
            answer.route = "modele_libre"
        restricted = set(plan.tables) & self.policy.restricted_tables
        if restricted and self.model is not None and getattr(self.model, "cloud", False) and answer.route.startswith("modele"):
            answer.status = "abstention"
            answer.message = f"Données restreintes ({', '.join(sorted(restricted))}) : réponse en local uniquement."
            self._record(answer, identity)
            return answer

        agree: bool | None = None
        distinct = {c.fingerprint: (i, c) for i, c in compiled}
        if len(distinct) > 1:
            first_sql = plan.sql
            for other_intent, other in list(distinct.values())[1:]:
                tables = sorted(set(plan.tables) | set(other.tables))
                counter = DistinguishingDB(self.graph).find(first_sql, other.sql, tables, dialect=plan.dialect)
                if counter is not None:
                    answer.status = "clarification"
                    answer.intent, answer.sql = intent, plan.sql
                    answer.alternatives = [self.explainer.explain(intent), self.explainer.explain(other_intent)]
                    answer.message = ("Deux interprétations donnent des résultats différents. Laquelle voulez-vous ?\n"
                                      f"Exemple qui les distingue :\n{counter.describe()}")
                    answer.timings_ms = timings
                    self._record(answer, identity)
                    return answer
            agree = True
        elif len(compiled) > 1:
            agree = True

        answer.intent, answer.sql = intent, plan.sql
        answer.interpretation = self.explainer.explain(intent)
        answer.assumptions, answer.rules = list(plan.assumptions), list(plan.rules)
        answer.warnings += plan.warnings
        flags: dict[str, bool] = {}
        if execute:
            t = time.perf_counter()
            try:
                result = self.source.query(plan.sql, max_rows=self.policy.max_rows)
            except SourceError as error:
                answer.status, answer.message = "erreur", f"La base a refusé la requête : {error}"
                answer.timings_ms = timings
                self._record(answer, identity)
                return answer
            timings["execution"] = round(1000 * (time.perf_counter() - t), 1)
            answer.columns, answer.rows = plan.columns or result.columns, result.rows
            checks = plausibility(result, plan, self.graph, intent.time.end if intent.time else None)
            answer.warnings += checks.warnings
            flags = checks.flags
            answer.chart = suggest_chart(answer.columns, answer.rows, int(plan.signals.get("dims", 0)),
                                         title=answer.interpretation)
        uncertified = sum(1 for m in intent.measures if m.metric and not self.graph.metric(m.metric).certified)
        answer.confidence = estimate(route=answer.route, coverage=coverage, compiled_signals=plan.signals,
                                     check_flags=flags, assumptions=len(plan.assumptions),
                                     candidates_agree=agree, uncertified=uncertified, calibrator=self.calibrator)
        answer.status = "ok" if answer.confidence.score >= self.min_confidence else "a_confirmer"
        if answer.route != "cache":
            self._cache[key] = (intent, answer.route, coverage)
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        timings["total"] = round(1000 * (time.perf_counter() - started), 1)
        answer.timings_ms = timings
        self._record(answer, identity)
        return answer

    async def aask(self, question: str, **kwargs: Any) -> Answer:
        return await asyncio.to_thread(self.ask, question, **kwargs)
