"""Mémoire des réponses récentes (suites de conversation, retours, export des résultats)."""

from __future__ import annotations

import threading
from collections import OrderedDict

from qraft import Answer
from qraft.text import phrase


class AnswerStore:
    def __init__(self, capacity: int = 2000) -> None:
        self.capacity = capacity
        self._items: OrderedDict[str, Answer] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, answer: Answer) -> None:
        with self._lock:
            self._items[answer.id] = answer
            self._items.move_to_end(answer.id)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def get(self, answer_id: str | None) -> Answer | None:
        if not answer_id:
            return None
        with self._lock:
            return self._items.get(answer_id)


def search_lexicon(engine, query: str, limit: int = 20) -> list[dict]:
    """Entrées du vocabulaire dont la forme canonique contient la requête."""
    needle = phrase(query)
    seen, found = set(), []
    for entry in engine.graph.lexicon():
        if needle and needle in entry.phrase and (entry.kind, entry.ref, entry.value) not in seen:
            seen.add((entry.kind, entry.ref, entry.value))
            found.append({"kind": entry.kind, "ref": entry.ref, "value": entry.value, "match": entry.phrase,
                          "label": engine.graph.label(entry.kind, entry.ref)})
            if len(found) >= limit:
                break
    return found
