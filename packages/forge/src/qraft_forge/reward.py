"""Récompense d'exécution pour l'apprentissage par renforcement (GRPO, étape 8 de la Forge).

Pour chaque complétion du modèle :
- exécution : 1.0 si l'intention décodée compile et donne le même résultat que la référence, 0 sinon ;
- format : +0.1 si le texte est un JSON QIR valide pour ce domaine (structure du QIR et
  identifiants réels du graphe, comme les énumérations de qir_json_schema) ;
- longueur : légère pénalité pour le texte qui dépasse la longueur du JSON de référence.

Le total est plafonné à 1.0 (une réponse juste vaut 1.0, une réponse bien formée mais fausse
vaut 0.1, comme les paliers d'Arctic-Text2SQL-R1) puis diminué de la pénalité, sans passer sous 0.

L'objet est appelable avec la signature des fonctions de récompense du GRPOTrainer de TRL :
reward(prompts=..., completions=..., reference=[...], **colonnes) -> list[float], où reference
est la colonne du jeu qui porte le JSON de l'intention attendue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import msgspec

from qraft.execute import Source
from qraft.graph import KnowledgeGraph
from qraft.qir import Intent, Measure

from .checks import ExecutionChecker, Rejected, results_match
from .dataset import intent_json

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def completion_text(completion: Any) -> str:
    """Texte d'une complétion TRL : chaîne (format standard) ou liste de messages (format conversationnel)."""
    if isinstance(completion, str):
        return completion
    if isinstance(completion, dict):
        return str(completion.get("content", ""))
    if isinstance(completion, (list, tuple)) and completion:
        last = completion[-1]
        return str(last.get("content", "")) if isinstance(last, dict) else str(last)
    return ""


def decode_intent(text: str) -> Intent | None:
    """Intention contenue dans un texte (JSON nu, bloc ```json, ou texte autour) ; None si illisible."""
    fenced = _FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(candidate[start:end + 1])
        if not isinstance(data, dict):
            return None
        return msgspec.convert({k: v for k, v in data.items() if v is not None}, Intent)
    except (json.JSONDecodeError, msgspec.ValidationError, TypeError):
        return None


def _as_intent(reference: Any) -> Intent:
    if isinstance(reference, Intent):
        return reference
    if isinstance(reference, dict):
        return msgspec.convert(reference, Intent)
    intent = decode_intent(str(reference))
    if intent is None:
        raise ValueError("référence illisible : JSON d'intention attendu")
    return intent


@dataclass
class RewardDetail:
    execution: float
    format: float
    length_penalty: float
    total: float


class ExecutionReward:
    """Récompense d'exécution ; références et résultats sont mis en cache par empreinte d'intention."""

    def __init__(self, graph: KnowledgeGraph, source: Source, *, format_bonus: float = 0.1,
                 length_coef: float = 0.05, max_length_penalty: float = 0.1,
                 checker: ExecutionChecker | None = None) -> None:
        self.graph = graph
        self.checker = checker or ExecutionChecker(graph, source)
        self.format_bonus = format_bonus
        self.length_coef = length_coef
        self.max_length_penalty = max_length_penalty
        self.__name__ = "recompense_execution"  # TRL nomme les récompenses dans ses journaux

    # --- Validité du QIR -------------------------------------------------------------------------

    def _column_ok(self, ref: str) -> bool:
        return ref in self.graph.semantic.dimensions or self.graph.catalog.resolve(ref) is not None

    def _measure_ok(self, m: Measure) -> bool:
        if m.metric is not None and m.metric not in self.graph.semantic.metrics:
            return False
        if m.column is not None and not (self.graph.catalog.table(m.column) if "." not in m.column
                                         else self.graph.catalog.resolve(m.column)):
            return False
        if m.metric is None and (m.agg is None or m.column is None):
            return False
        return all(self._column_ok(f.column) for f in m.filters)

    def is_valid(self, intent: Intent) -> bool:
        """Le QIR ne cite que des identifiants réels du graphe (ce qu'imposent les enums du schéma)."""
        measures = list(intent.measures) + [m for r in intent.ratios for m in (r.numerator, r.denominator)]
        if not measures and not intent.dimensions and not intent.escape_sql:
            return False
        return (all(self._measure_ok(m) for m in measures)
                and all(d.column == "time" or self._column_ok(d.column) for d in intent.dimensions)
                and all(self._column_ok(f.column) for f in intent.filters)
                and (intent.time is None or intent.time.column is None
                     or self.graph.catalog.resolve(intent.time.column) is not None))

    # --- Récompense ---------------------------------------------------------------------------------

    def score(self, completion: Any, reference: Any) -> RewardDetail:
        expected = _as_intent(reference)
        text = completion_text(completion).strip()
        intent = decode_intent(text)
        valid = intent is not None and self.is_valid(intent)
        execution = 0.0
        if valid and intent is not None:
            try:
                ref = self.checker.run(expected)
            except Rejected:
                ref = None  # référence inexploitable : aucune récompense d'exécution possible
            if ref is not None:
                if intent.fingerprint() == expected.fingerprint():
                    execution = 1.0
                else:
                    try:
                        execution = 1.0 if results_match(ref, self.checker.run(intent)) else 0.0
                    except Rejected:
                        execution = 0.0
        target = len(intent_json(expected))
        excess = max(0, len(text) - target)
        penalty = min(self.max_length_penalty, self.length_coef * excess / max(target, 1))
        bonus = self.format_bonus if valid else 0.0
        total = max(0.0, min(1.0, execution + bonus) - penalty)
        return RewardDetail(execution, bonus, round(penalty, 6), round(total, 6))

    def __call__(self, prompts: Any = None, completions: list[Any] | None = None,
                 reference: list[Any] | None = None, **kwargs: Any) -> list[float]:
        if completions is None:
            return []
        if reference is None:
            raise ValueError("colonne « reference » absente : le jeu GRPO doit porter le JSON de l'intention attendue")
        return [self.score(c, r).total for c, r in zip(completions, reference, strict=True)]
