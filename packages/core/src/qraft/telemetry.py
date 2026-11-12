"""Traces OpenTelemetry (optionnelles) : un span par question, avec route, statut, confiance et durées.

Sans le paquet opentelemetry-api, tout est sans effet. L'export (OTLP, console...) se
configure côté application avec le SDK OpenTelemetry habituel.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace as _trace

    _TRACER = _trace.get_tracer("qraft")
except ImportError:  # pragma: no cover - dépend de l'environnement
    _TRACER = None


def enabled() -> bool:
    return _TRACER is not None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    if _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(f"qraft.{key}", value)
        yield current


def annotate(current: Any, **attributes: Any) -> None:
    if current is None:
        return
    for key, value in attributes.items():
        if isinstance(value, (str, bool, int, float)):
            current.set_attribute(f"qraft.{key}", value)
