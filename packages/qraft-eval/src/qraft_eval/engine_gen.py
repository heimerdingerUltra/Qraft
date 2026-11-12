"""Générateur « moteur Qraft » : mesure le moteur avec le même protocole que la baseline.

Le moteur ne fait ici qu'interpréter et compiler ; le harnais exécute et compare,
exactement comme pour la baseline. Le moteur s'abstient quand il n'est pas sûr.
"""

from __future__ import annotations

import time
from typing import Any

from .baseline import Prediction


class EngineGenerator:
    def __init__(self, engine: Any, *, label: str = "qraft-moteur", count_unsure_as_answer: bool = True) -> None:
        self.engine = engine
        self.model = label
        self.count_unsure_as_answer = count_unsure_as_answer

    def generate(self, question: str) -> Prediction:
        started = time.perf_counter()
        answer = self.engine.ask(question, execute=False)
        answered = answer.status == "ok" or (self.count_unsure_as_answer and answer.status == "a_confirmer")
        notes = list(answer.assumptions)
        if answer.message and not answered:
            notes.append(answer.message)
        return Prediction(
            answerable=bool(answered and answer.sql),
            sql=answer.sql or "",
            assumptions=notes,
            model=self.model,
            stop_reason=answer.route or answer.status,
            latency_s=time.perf_counter() - started,
        )
