"""Routeur appris : faut-il faire confiance au parseur à règles, ou passer la question au modèle ?

Régression logistique (Python pur, aucune dépendance) sur des signaux du parseur, entraînée
sur les questions étiquetées (jeu or, retours utilisateurs). Décision en moins d'une milliseconde,
sans appel de modèle sur le chemin critique.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .parser import ParseResult, RuleParser

FEATURES = ["biais", "couverture", "mots_inconnus", "mesures", "dimensions", "filtres", "periode", "comparaison",
            "ratio", "classement", "suite", "longueur"]


def features(parse: ParseResult, question: str) -> list[float]:
    intent = parse.intent
    return [
        1.0,
        parse.coverage,
        min(len(parse.unmatched), 5) / 5,
        min(len(intent.measures), 3) / 3,
        min(len(intent.dimensions), 3) / 3,
        min(len(intent.filters), 3) / 3,
        1.0 if intent.time else 0.0,
        1.0 if intent.time and intent.time.compare else 0.0,
        1.0 if intent.ratios else 0.0,
        1.0 if intent.ranking else 0.0,
        1.0 if parse.followup else 0.0,
        min(len(question.split()), 40) / 40,
    ]


@dataclass
class LearnedRouter:
    threshold: float = 0.7
    weights: list[float] = field(default_factory=lambda: [0.0] * len(FEATURES))

    def predict(self, x: list[float]) -> float:
        z = sum(w * v for w, v in zip(self.weights, x))
        return 1 / (1 + math.exp(-max(min(z, 30), -30)))

    def trust_rules(self, parse: ParseResult, question: str) -> bool:
        return parse.handled and self.predict(features(parse, question)) >= self.threshold

    def fit(self, samples: Iterable[tuple[list[float], bool]], *, epochs: int = 400, lr: float = 0.5,
            l2: float = 1e-3) -> LearnedRouter:
        data = list(samples)
        if not data:
            return self
        n = len(data)
        for _ in range(epochs):
            grad = [0.0] * len(self.weights)
            for x, y in data:
                error = self.predict(x) - (1.0 if y else 0.0)
                for i, v in enumerate(x):
                    grad[i] += error * v
            self.weights = [w - lr * (g / n + l2 * w * (i > 0)) for i, (w, g) in enumerate(zip(self.weights, grad))]
        return self

    @classmethod
    def train(cls, parser: RuleParser, labeled: Iterable[tuple[str, bool]], **kwargs: float) -> LearnedRouter:
        """labeled : (question, la réponse des règles était-elle juste ?)."""
        samples = []
        for question, correct in labeled:
            parse = parser.parse(question)
            if parse.handled:
                samples.append((features(parse, question), correct))
        threshold = float(kwargs.pop("threshold", 0.7))
        return cls(threshold=threshold).fit(samples, **kwargs)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps({"features": FEATURES, "weights": self.weights, "threshold": self.threshold},
                                   indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> LearnedRouter:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("features") != FEATURES:
            raise ValueError("routeur entraîné sur d'autres signaux : réentraîner")
        return cls(threshold=data["threshold"], weights=data["weights"])
