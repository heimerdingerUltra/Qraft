"""Baseline de la phase 0 : un grand modèle, le schéma complet, aucune autre aide.

Chaque composant de Qraft devra battre ce chiffre pour justifier sa complexité.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.engine import Engine

from .compare import compare_results
from .db import run_query
from .goldset import GoldQuestion, GoldSet
from .safety import UnsafeSQL, check_read_only, has_top_level_order_by

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
# Repli côté serveur : si le modèle décline, l'API rejoue la requête sur un modèle de repli.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "sql": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answerable", "sql", "assumptions"],
    "additionalProperties": False,
}

SYSTEM_TEMPLATE = """Tu es un expert SQL. Tu traduis une question métier en une seule requête SQL de lecture.

Dialecte SQL : {dialect}

Règles :
- Une seule instruction SELECT ; les CTE (WITH) sont autorisées. Jamais d'écriture ni de DDL.
- N'utilise que les tables et colonnes du schéma ci-dessous.
- Si le schéma ne permet pas de répondre, mets answerable à false et sql à une chaîne vide.
- Mets dans assumptions chaque hypothèse d'interprétation que tu as dû faire.

Schéma de la base :

{schema}"""


def build_system_prompt(schema_text: str, dialect: str) -> str:
    return SYSTEM_TEMPLATE.format(dialect=dialect, schema=schema_text.strip())


@dataclass
class Prediction:
    answerable: bool
    sql: str
    assumptions: list[str] = field(default_factory=list)
    model: str | None = None
    stop_reason: str | None = None
    refused: bool = False
    error: str | None = None
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0


class SqlGenerator(Protocol):
    model: str

    def generate(self, question: str) -> Prediction: ...


class ClaudeGenerator:
    """Générateur de SQL appuyé sur Claude, via le SDK officiel anthropic."""

    def __init__(
        self,
        system_prompt: str,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        fallbacks: bool = True,
        client: Any | None = None,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()  # identifiants lus dans l'environnement
        self.client = client
        self.system_prompt = system_prompt
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks

    def _request(self, question: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": 16000,
            # Préfixe stable (consignes + schéma) mis en cache une heure : seules les questions changent.
            "system": [
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            "messages": [{"role": "user", "content": question}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        }

    def generate(self, question: str) -> Prediction:
        import anthropic

        started = time.perf_counter()
        try:
            if self.fallbacks:
                response = self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **self._request(question)
                )
            else:
                response = self.client.messages.create(**self._request(question))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError):
            raise  # identifiants, droits ou modèle invalides : inutile de continuer le run
        except anthropic.RateLimitError as error:
            return Prediction(False, "", error=f"limite de débit atteinte : {error.message}",
                              latency_s=time.perf_counter() - started)
        except anthropic.APIStatusError as error:
            return Prediction(False, "", error=f"erreur API {error.status_code} : {error.message}",
                              latency_s=time.perf_counter() - started)
        except anthropic.APIConnectionError:
            return Prediction(False, "", error="connexion à l'API impossible",
                              latency_s=time.perf_counter() - started)

        usage = response.usage
        meta = {
            "model": response.model,
            "stop_reason": response.stop_reason,
            "input_tokens": usage.input_tokens or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "output_tokens": usage.output_tokens or 0,
            "latency_s": time.perf_counter() - started,
        }
        if response.stop_reason == "refusal":
            return Prediction(False, "", refused=True, **meta)
        if response.stop_reason == "max_tokens":
            return Prediction(False, "", error="réponse tronquée (max_tokens)", **meta)
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return Prediction(False, "", error="réponse JSON invalide", **meta)
        return Prediction(
            answerable=bool(data.get("answerable")),
            sql=(data.get("sql") or "").strip(),
            assumptions=[str(a) for a in data.get("assumptions") or []],
            **meta,
        )


# Statuts possibles d'une question évaluée.
CORRECT = "correct"
INCORRECT = "incorrect"
SQL_ERROR = "erreur_sql"
SQL_REJECTED = "sql_refuse"
WRONG_ABSTENTION = "abstention_a_tort"
RIGHT_ABSTENTION = "abstention_correcte"
WRONG_ANSWER = "reponse_a_tort"
REFUSED = "refus"
API_ERROR = "erreur_api"


@dataclass
class QuestionResult:
    id: str
    question: str
    difficulty: str | None
    tags: list[str]
    expected_answerable: bool
    status: str
    reason: str
    predicted_sql: str
    assumptions: list[str]
    model: str | None
    latency_s: float
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    gold_preview: list[list[Any]] = field(default_factory=list)
    pred_preview: list[list[Any]] = field(default_factory=list)


def _first_line(error: Exception, limit: int = 300) -> str:
    message = str(error).strip().splitlines()
    return (message[0] if message else type(error).__name__)[:limit]


def evaluate_one(dialect: str, engine: Engine, q: GoldQuestion, pred: Prediction, *, max_rows: int) -> QuestionResult:
    """Classe une prédiction et, si elle répond, compare son résultat à la référence.

    La requête prédite est exécutée telle quelle (on mesure le modèle), après le contrôle
    de lecture seule. La base doit être ouverte avec un compte sans droit d'écriture.
    """
    result = QuestionResult(
        id=q.id, question=q.question, difficulty=q.difficulty, tags=list(q.tags),
        expected_answerable=q.answerable, status="", reason="", predicted_sql=pred.sql,
        assumptions=pred.assumptions, model=pred.model, latency_s=round(pred.latency_s, 3),
        input_tokens=pred.input_tokens, cache_read_tokens=pred.cache_read_tokens,
        cache_write_tokens=pred.cache_write_tokens, output_tokens=pred.output_tokens,
    )
    if pred.error:
        result.status, result.reason = API_ERROR, pred.error
    elif pred.refused:
        result.status, result.reason = REFUSED, "le modèle a décliné la demande"
    elif not q.answerable:
        result.status = RIGHT_ABSTENTION if not pred.answerable else WRONG_ANSWER
    elif not pred.answerable or not pred.sql:
        result.status, result.reason = WRONG_ABSTENTION, "le modèle a jugé la question sans réponse"
    else:
        try:
            check_read_only(pred.sql, dialect)
        except UnsafeSQL as error:
            result.status, result.reason = SQL_REJECTED, str(error)
            return result
        try:
            pred_res = run_query(engine, pred.sql, max_rows=max_rows)
        except Exception as error:  # noqa: BLE001 - toute erreur de la base est un échec de la prédiction
            result.status, result.reason = SQL_ERROR, _first_line(error)
            return result
        gold_tree = check_read_only(q.sql or "", dialect)
        gold_res = run_query(engine, q.sql or "", max_rows=max_rows)
        ordered = q.ordered if q.ordered is not None else has_top_level_order_by(gold_tree)
        comparison = compare_results(gold_res.rows, pred_res.rows, ordered=ordered)
        result.status = CORRECT if comparison.match else INCORRECT
        result.reason = comparison.reason
        result.gold_preview = [list(r) for r in gold_res.rows[:5]]
        result.pred_preview = [list(r) for r in pred_res.rows[:5]]
    return result


def run_baseline(
    goldset: GoldSet,
    engine: Engine,
    generator: SqlGenerator,
    *,
    concurrency: int = 4,
    limit: int | None = None,
    max_rows: int = 100_000,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[QuestionResult]:
    """Génère les prédictions en parallèle, puis les évalue une à une sur la base."""
    questions = goldset.questions[:limit] if limit else list(goldset.questions)
    predictions: dict[str, Prediction] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(generator.generate, q.question): q.id for q in questions}
        for done, future in enumerate(as_completed(futures), start=1):
            predictions[futures[future]] = future.result()
            if on_progress:
                on_progress(done, len(questions), futures[future])
    return [evaluate_one(goldset.dialect, engine, q, predictions[q.id], max_rows=max_rows) for q in questions]
