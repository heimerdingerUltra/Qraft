"""Validation d'un jeu or contre sa base : chaque référence doit s'exécuter et discriminer."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from .db import run_query
from .goldset import GoldSet
from .safety import UnsafeSQL, check_read_only

ERROR = "erreur"
WARNING = "alerte"


@dataclass(frozen=True)
class Issue:
    question_id: str
    level: str
    message: str


def validate_goldset(goldset: GoldSet, engine: Engine, *, max_rows: int = 100_000) -> tuple[list[Issue], dict[str, int]]:
    """Retourne les problèmes trouvés et le nombre de lignes de chaque référence."""
    issues: list[Issue] = []
    row_counts: dict[str, int] = {}
    for q in goldset.questions:
        if not q.answerable:
            continue
        try:
            check_read_only(q.sql or "", goldset.dialect)
        except UnsafeSQL as error:
            issues.append(Issue(q.id, ERROR, f"référence refusée : {error}"))
            continue
        try:
            result = run_query(engine, q.sql or "", max_rows=max_rows)
        except Exception as error:  # noqa: BLE001 - on remonte l'erreur de la base telle quelle
            issues.append(Issue(q.id, ERROR, f"la référence échoue : {str(error).strip().splitlines()[0][:300]}"))
            continue
        row_counts[q.id] = len(result.rows)
        if not result.rows:
            issues.append(Issue(q.id, WARNING, "résultat vide : la comparaison sera peu discriminante"))
        if result.truncated:
            issues.append(Issue(q.id, WARNING, f"résultat tronqué à {max_rows} lignes"))

    total = len(goldset.questions)
    unanswerable = sum(not q.answerable for q in goldset.questions)
    if total and unanswerable / total < 0.05:
        issues.append(Issue("*", WARNING, f"seulement {unanswerable} question(s) sans réponse sur {total} : viser environ 10 %"))
    missing_difficulty = sum(q.difficulty is None for q in goldset.questions)
    if missing_difficulty:
        issues.append(Issue("*", WARNING, f"{missing_difficulty} question(s) sans difficulté renseignée"))
    return issues, row_counts
