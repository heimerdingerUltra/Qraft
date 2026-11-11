"""Comparaison de résultats de requêtes (précision d'exécution).

Deux résultats sont équivalents s'ils contiennent les mêmes lignes, après
normalisation des valeurs (nombres à 10 chiffres significatifs, dates en ISO).
L'ordre des lignes ne compte que si la référence trie son résultat. L'ordre des
colonnes peut différer : on cherche une permutation qui aligne les colonnes.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

# Au-delà, on ne cherche pas de permutation de colonnes (coût combinatoire).
MAX_PERMUTATION_WIDTH = 8


@dataclass(frozen=True)
class Comparison:
    match: bool
    reason: str = ""


def normalize_value(value: Any) -> Any:
    """Ramène une valeur de base à une forme comparable entre dialectes et drivers."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "inf" if number > 0 else "-inf"
        return float(f"{number:.10g}")
    if isinstance(value, datetime):
        if value.tzinfo is None and value.time() == time(0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value if isinstance(value, str) else str(value)


def normalize_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[Any, ...]]:
    return [tuple(normalize_value(v) for v in row) for row in rows]


def _same(gold: list[tuple[Any, ...]], pred: list[tuple[Any, ...]], ordered: bool) -> bool:
    return gold == pred if ordered else Counter(gold) == Counter(pred)


def _permutations(candidates: list[list[int]]) -> Iterator[tuple[int, ...]]:
    """Énumère les affectations colonne-référence -> colonne-prédiction sans doublon."""
    chosen: list[int] = []
    used: set[int] = set()

    def walk(i: int) -> Iterator[tuple[int, ...]]:
        if i == len(candidates):
            yield tuple(chosen)
            return
        for j in candidates[i]:
            if j in used:
                continue
            used.add(j)
            chosen.append(j)
            yield from walk(i + 1)
            chosen.pop()
            used.discard(j)

    yield from walk(0)


def compare_results(
    gold_rows: Sequence[Sequence[Any]],
    pred_rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
) -> Comparison:
    """Compare le résultat de référence et le résultat prédit."""
    gold = normalize_rows(gold_rows)
    pred = normalize_rows(pred_rows)
    if len(gold) != len(pred):
        return Comparison(False, f"nombre de lignes différent : attendu {len(gold)}, obtenu {len(pred)}")
    if not gold:
        return Comparison(True, "deux résultats vides")
    width = len(gold[0])
    if len(pred[0]) != width:
        return Comparison(False, f"nombre de colonnes différent : attendu {width}, obtenu {len(pred[0])}")
    if _same(gold, pred, ordered):
        return Comparison(True)

    if 1 < width <= MAX_PERMUTATION_WIDTH:
        gold_columns = [Counter(row[i] for row in gold) for i in range(width)]
        pred_columns = [Counter(row[j] for row in pred) for j in range(width)]
        candidates = [[j for j in range(width) if pred_columns[j] == gold_columns[i]] for i in range(width)]
        if all(candidates):
            for perm in _permutations(candidates):
                if perm == tuple(range(width)):
                    continue
                reordered = [tuple(row[j] for j in perm) for row in pred]
                if _same(gold, reordered, ordered):
                    return Comparison(True, "colonnes dans un autre ordre")

    if ordered and Counter(gold) == Counter(pred):
        return Comparison(False, "mêmes lignes, mais pas dans l'ordre attendu")
    return Comparison(False, "valeurs différentes")
