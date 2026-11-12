"""Modèles qui proposent des intentions QIR : professeur cloud (Claude), élève local, statique.

Le modèle ne produit jamais de SQL final : il remplit le QIR, contraint par un
schéma JSON dont les énumérations ne contiennent que des identifiants réels du graphe.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

import msgspec

from .graph import KnowledgeGraph
from .qir import Intent


class ModelPort(Protocol):
    name: str
    cloud: bool

    def propose(self, question: str, *, previous: Intent | None = None, n: int = 1) -> list[Intent]: ...


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def qir_json_schema(graph: KnowledgeGraph) -> dict[str, Any]:
    """Schéma JSON du QIR pour ce graphe. Stable tant que le graphe ne change pas (grammaire mise en cache)."""
    columns = sorted(f"{t.name}.{c.name}" for t in graph.catalog.tables.values() for c in t.columns)
    tables = sorted(graph.catalog.tables)
    metrics = sorted(graph.semantic.metrics)
    dimensions = sorted(graph.semantic.dimensions)
    scalar = {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
                        {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}]}
    filter_schema = _obj({
        "column": {"type": "string", "enum": dimensions + columns},
        "op": {"type": "string", "enum": ["=", "!=", "<", "<=", ">", ">=", "in", "not_in", "between",
                                          "contains", "is_null", "not_null"]},
        "value": scalar,
    })
    measure = _obj({
        "metric": _nullable({"type": "string", "enum": metrics}) if metrics else {"type": "null"},
        "agg": _nullable({"type": "string", "enum": ["sum", "count", "count_distinct", "avg", "min", "max"]}),
        "column": _nullable({"type": "string", "enum": columns + tables}),
        "filters": {"type": "array", "items": filter_schema},
        "alias": _nullable({"type": "string"}),
    })
    return _obj({
        "measures": {"type": "array", "items": measure},
        "ratios": {"type": "array", "items": _obj({
            "numerator": measure, "denominator": measure, "percent": {"type": "boolean"},
            "alias": _nullable({"type": "string"})})},
        "dimensions": {"type": "array", "items": _obj({
            "column": {"type": "string", "enum": ["time"] + dimensions + columns},
            "grain": _nullable({"type": "string", "enum": ["day", "week", "month", "quarter", "year"]}),
            "alias": _nullable({"type": "string"})})},
        "filters": {"type": "array", "items": filter_schema},
        "time": _nullable(_obj({
            "start": _nullable({"type": "string"}), "end": _nullable({"type": "string"}),
            "column": _nullable({"type": "string", "enum": columns}),
            "compare": _nullable({"type": "string", "enum": ["previous_year", "previous_period"]}),
            "compare_output": {"type": "string", "enum": ["all", "pct"]},
            "label": _nullable({"type": "string"})})),
        "ranking": _nullable(_obj({
            "by": _nullable({"type": "string"}), "direction": {"type": "string", "enum": ["desc", "asc"]},
            "limit": _nullable({"type": "integer"})})),
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "escape_sql": _nullable({"type": "string"}),
        "needs_context": {"type": "array", "items": {"type": "string"}},
        "answerable": {"type": "boolean"},
    })


INSTRUCTIONS = """Tu traduis une question métier en intention QIR (JSON), jamais en SQL final.

Règles :
- Utilise d'abord les métriques certifiées et les dimensions du glossaire ; sinon un agrégat sur une colonne.
- Pour compter les lignes d'une table : agg "count" et column = nom de la table.
- Une dimension "time" avec un grain regroupe par période sur la colonne temporelle de la mesure.
- La période est [start, end[ en dates ISO. "compare" ajoute la période précédente.
- N'écris jamais de jointure : le compilateur les calcule.
- Mets dans assumptions chaque hypothèse d'interprétation.
- Si la question est impossible avec ces données, answerable = false.
- escape_sql (SQL de lecture, dialecte indiqué) seulement si le QIR ne peut pas exprimer la question.
- Si une information te manque, liste-la dans needs_context au lieu d'inventer.
"""


def _decode(text: str) -> Intent | None:
    try:
        data = json.loads(text)
        data = {k: v for k, v in data.items() if v is not None}
        return msgspec.convert(data, Intent)
    except (json.JSONDecodeError, msgspec.ValidationError, AttributeError, TypeError):
        return None


class ClaudeModel:
    """Professeur cloud : Claude via le SDK officiel anthropic, sorties structurées."""

    name = "claude"
    cloud = True

    def __init__(self, graph: KnowledgeGraph, *, model: str = "claude-opus-5-5", effort: str = "medium",
                 fallbacks: bool = True, client: Any | None = None, hidden_tables: set[str] | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self.schema = qir_json_schema(graph)
        summary = graph.summary_for_prompt()
        if hidden_tables:  # données restreintes : leurs valeurs ne partent jamais vers le cloud
            summary = "\n".join(l for l in summary.splitlines()
                                if not any(l.startswith(f"- {t}(") for t in hidden_tables))
        self.system = INSTRUCTIONS + "\n" + summary

    def _one(self, content: str) -> Intent | None:
        import anthropic

        request = {
            "model": self.model,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            "messages": [{"role": "user", "content": content}],
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": self.schema}},
        }
        try:
            if self.fallbacks:
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
            else:
                response = self.client.messages.create(**request)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError):
            raise
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError):
            return None
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        return _decode(next((b.text for b in response.content if b.type == "text"), ""))

    def propose(self, question: str, *, previous: Intent | None = None, n: int = 1) -> list[Intent]:
        content = question
        if previous is not None:
            content = (f"Intention précédente (la question est une suite) :\n"
                       f"{msgspec.json.encode(previous).decode()}\n\nQuestion : {question}")
        with ThreadPoolExecutor(max_workers=max(1, n)) as pool:
            results = list(pool.map(self._one, [content] * n))
        return [r for r in results if r is not None]


class LocalModel:
    """Élève local servi par vLLM (API compatible OpenAI), sortie contrainte par le schéma QIR."""

    name = "local"
    cloud = False

    def __init__(self, graph: KnowledgeGraph, *, base_url: str = "http://localhost:8000", model: str = "qraft-student",
                 timeout_s: float = 30.0, client: Any | None = None) -> None:
        if client is None:
            import httpx

            client = httpx.Client(base_url=base_url, timeout=timeout_s)
        self.client = client
        self.model = model
        self.schema = qir_json_schema(graph)
        self.system = INSTRUCTIONS + "\n" + graph.summary_for_prompt()

    def propose(self, question: str, *, previous: Intent | None = None, n: int = 1) -> list[Intent]:
        user = question if previous is None else (
            f"Intention précédente : {msgspec.json.encode(previous).decode()}\nQuestion : {question}")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "qir", "schema": self.schema}},
            "temperature": 0.0 if n == 1 else 0.7,
            "n": n,
        }
        try:
            response = self.client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            choices = response.json().get("choices", [])
        except Exception:  # noqa: BLE001 - service local indisponible : pas de proposition
            return []
        return [i for c in choices if (i := _decode(c.get("message", {}).get("content", ""))) is not None]


class StaticModel:
    """Modèle de test : renvoie des intentions prédéfinies par question."""

    name = "statique"

    def __init__(self, answers: dict[str, list[Intent]], *, cloud: bool = False) -> None:
        self.answers = answers
        self.cloud = cloud

    def propose(self, question: str, *, previous: Intent | None = None, n: int = 1) -> list[Intent]:
        return list(self.answers.get(question, []))
