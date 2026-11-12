"""Visualisation automatique : règles déterministes d'abord, spécification Vega-Lite."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _is_temporal(values: list[Any]) -> bool:
    present = [v for v in values if v is not None]
    return bool(present) and all(isinstance(v, (date, datetime)) or (isinstance(v, str) and _ISO_DATE.match(v))
                                 for v in present)


def _is_numeric(values: list[Any]) -> bool:
    present = [v for v in values if v is not None]
    return bool(present) and all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in present)


def suggest_chart(columns: list[str], rows: list[tuple[Any, ...]], n_dims: int, title: str | None = None) -> dict | None:
    """Spécification Vega-Lite pour le résultat, ou None si un tableau suffit."""
    if not rows or len(columns) < 2 or n_dims == 0:
        return None
    dims, measures = columns[:n_dims], columns[n_dims:]
    numeric = [m for i, m in enumerate(measures) if _is_numeric([r[n_dims + i] for r in rows])]
    if not numeric:
        return None
    data = [{c: _json_value(v) for c, v in zip(columns, r)} for r in rows]
    first = [r[0] for r in rows]
    temporal = _is_temporal(first)
    spec: dict[str, Any] = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                            "data": {"values": data}}
    if title:
        spec["title"] = title
    y = {"field": numeric[0], "type": "quantitative", "title": numeric[0]}
    if n_dims == 1 and temporal:
        spec.update(mark={"type": "line", "point": True},
                    encoding={"x": {"field": dims[0], "type": "temporal", "title": dims[0]}, "y": y})
    elif n_dims == 1 and len(rows) <= 30:
        spec.update(mark="bar", encoding={
            "y": {"field": dims[0], "type": "nominal", "sort": "-x", "title": dims[0]},
            "x": {**y}})
    elif n_dims == 2:
        x = {"field": dims[0], "type": "temporal" if temporal else "nominal", "title": dims[0]}
        mark = "line" if temporal else "bar"
        encoding: dict[str, Any] = {"x": x, "y": y, "color": {"field": dims[1], "type": "nominal"}}
        if not temporal:
            encoding["xOffset"] = {"field": dims[1]}
        spec.update(mark=mark, encoding=encoding)
    else:
        return None
    return spec
