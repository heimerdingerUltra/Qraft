"""Confiance : signaux combinés en une probabilité d'exactitude, puis calibrée.

Les poids initiaux sont posés à la main ; Calibrator les recale sur les réponses
étiquetées (jeu or, revues des stewards) par régression isotone.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

TIERS = ((0.85, "vérifié"), (0.6, "probable"), (0.0, "incertain"))

ROUTE_PRIOR = {"cache": 0.6, "regles": 0.3, "modele": 0.0, "modele_libre": -0.8}


@dataclass
class Confidence:
    score: float
    tier: str
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


def tier_of(score: float) -> str:
    return next(name for threshold, name in TIERS if score >= threshold)


def estimate(
    *,
    route: str,
    coverage: float,
    compiled_signals: dict[str, Any],
    check_flags: dict[str, bool],
    assumptions: int,
    candidates_agree: bool | None = None,
    uncertified: int = 0,
    calibrator: Calibrator | None = None,
) -> Confidence:
    reasons: list[str] = []
    z = 2.2 + ROUTE_PRIOR.get(route, 0.0)
    if coverage < 1:
        z -= 3.0 * (1 - coverage)
        reasons.append(f"{round(100 * (1 - coverage))} % des mots non reconnus")
    for flag, weight, reason in (
        ("ambiguous_join", 1.2, "chemin de jointure ambigu"),
        ("fanout", 1.0, "dimension plus fine que la mesure"),
        ("escape", 0.8, "SQL libre non vérifié par le compilateur"),
    ):
        if compiled_signals.get(flag):
            z -= weight
            reasons.append(reason)
    inferred = min(int(compiled_signals.get("inferred_edges", 0)), 2)
    if inferred:
        z -= 0.6 * inferred
        reasons.append("relation inférée, non déclarée")
    for flag, weight, reason in (
        ("empty", 1.5, "résultat vide"),
        ("nulls", 0.8, "valeurs vides nombreuses"),
        ("stale", 0.5, "données possiblement incomplètes"),
        ("truncated", 0.2, "résultat tronqué"),
    ):
        if check_flags.get(flag):
            z -= weight
            reasons.append(reason)
    if assumptions:
        z -= 0.4 * min(assumptions, 3)
        reasons.append(f"{assumptions} hypothèse(s) d'interprétation")
    if candidates_agree is False:
        z -= 2.0
        reasons.append("les interprétations candidates divergent")
    elif candidates_agree is True:
        z += 0.5
    if uncertified:
        z -= 0.7
        reasons.append("métrique non certifiée")
    score = 1 / (1 + math.exp(-z))
    if calibrator is not None:
        score = calibrator.transform(score)
    return Confidence(round(score, 4), tier_of(score), reasons,
                      {"route": route, "coverage": round(coverage, 3), **compiled_signals, **check_flags})


class Calibrator:
    """Régression isotone (algorithme PAV) : score brut -> probabilité observée d'exactitude."""

    def __init__(self) -> None:
        self.thresholds: list[float] = []
        self.values: list[float] = []

    def fit(self, scores: list[float], labels: list[bool]) -> Calibrator:
        pairs = sorted(zip(scores, labels))
        blocks: list[list[float]] = []  # [somme des labels, effectif, score max]
        for s, y in pairs:
            blocks.append([float(y), 1.0, s])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
                total, count, top = blocks.pop()
                blocks[-1][0] += total
                blocks[-1][1] += count
                blocks[-1][2] = top
        self.thresholds = [b[2] for b in blocks]
        self.values = [b[0] / b[1] for b in blocks]
        return self

    def transform(self, score: float) -> float:
        if not self.thresholds:
            return score
        i = min(bisect_right(self.thresholds, score), len(self.values) - 1)
        return self.values[i]

    @staticmethod
    def expected_calibration_error(scores: list[float], labels: list[bool], bins: int = 10) -> float:
        total, error = len(scores), 0.0
        for b in range(bins):
            lo, hi = b / bins, (b + 1) / bins
            idx = [i for i, s in enumerate(scores) if lo <= s < hi or (b == bins - 1 and s == 1.0)]
            if idx:
                confidence = sum(scores[i] for i in idx) / len(idx)
                accuracy = sum(labels[i] for i in idx) / len(idx)
                error += len(idx) / total * abs(confidence - accuracy)
        return error
