"""Acquisition de connaissance : import depuis dbt (manifest.json, MetricFlow) et fusion avec conflits.

Les faits importés ne sont pas certifiés d'office : ils entrent comme candidats, et
toute définition concurrente d'une définition existante devient un conflit à arbitrer
par un steward.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from .semantic import Dimension, Entity, Metric, SemanticLayer

_AGG_SQL = {
    "sum": "SUM({x})", "count": "COUNT({x})", "count_distinct": "COUNT(DISTINCT {x})", "average": "AVG({x})",
    "avg": "AVG({x})", "min": "MIN({x})", "max": "MAX({x})", "median": "MEDIAN({x})",
    "sum_boolean": "SUM(CASE WHEN {x} THEN 1 ELSE 0 END)",
}
_DBT_DIMENSION = re.compile(r"\{\{\s*(?:Dimension|TimeDimension|Entity)\(\s*'([^']+)'[^)]*\)\s*\}\}")


@dataclass
class Conflict:
    kind: str  # metric | dimension
    id: str
    existing: str
    incoming: str
    source: str = ""


@dataclass
class ImportReport:
    layer: SemanticLayer
    warnings: list[str] = field(default_factory=list)


def _table_of(semantic_model: dict[str, Any]) -> str:
    relation = semantic_model.get("node_relation") or {}
    return relation.get("alias") or semantic_model.get("name", "")


def _column_ref(table: str, expr: str) -> str:
    return expr if "." in expr or "(" in expr else f"{table}.{expr}"


def import_dbt_manifest(path: str | Path, *, domain: str = "dbt") -> ImportReport:
    """Construit une couche sémantique candidate depuis un manifest dbt (modèles + MetricFlow)."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    warnings: list[str] = []
    entities: dict[str, Entity] = {}
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") == "model":
            name = node.get("alias") or node["name"]
            entities[name] = Entity(table=name, label=node.get("description") or name)

    measures: dict[str, tuple[str, str, str | None]] = {}  # nom -> (expression SQL, table, colonne temporelle)
    dimensions: dict[str, Dimension] = {}
    for sm in manifest.get("semantic_models", {}).values():
        table = _table_of(sm)
        time_default = (sm.get("defaults") or {}).get("agg_time_dimension")
        dims_by_name = {}
        for dim in sm.get("dimensions", []):
            column = _column_ref(table, dim.get("expr") or dim["name"])
            dims_by_name[dim["name"]] = column
            if dim.get("type") != "time":
                dimensions[dim["name"]] = Dimension(id=dim["name"], column=column, label=dim.get("label") or dim["name"])
        for measure in sm.get("measures", []):
            template = _AGG_SQL.get(str(measure.get("agg", "")).lower())
            if template is None:
                warnings.append(f"agrégat non pris en charge pour la mesure {measure.get('name')}")
                continue
            expr = measure.get("expr") or measure["name"]
            target = "*" if expr in ("1", "*") and measure.get("agg") == "count" else _column_ref(table, str(expr))
            time_dim = measure.get("agg_time_dimension") or time_default
            measures[measure["name"]] = (template.format(x=target), table, dims_by_name.get(time_dim) if time_dim else None)

    metrics: dict[str, Metric] = {}
    for m in manifest.get("metrics", {}).values():
        name, kind, params = m["name"], m.get("type", "simple"), m.get("type_params") or {}
        if kind == "simple":
            measure = params.get("measure")
            measure_name = measure.get("name") if isinstance(measure, dict) else measure
            if measure_name not in measures:
                warnings.append(f"métrique {name} : mesure {measure_name} introuvable")
                continue
            expression, table, time_column = measures[measure_name]
            filters = []
            for where in ((m.get("filter") or {}).get("where_filters") or []):
                template = where.get("where_sql_template", "")
                sql = _DBT_DIMENSION.sub(lambda g: _column_ref(table, g.group(1).split("__")[-1]), template)
                if "{{" in sql:
                    warnings.append(f"métrique {name} : filtre non traduit ({template})")
                else:
                    filters.append(sql)
            metrics[name] = Metric(id=name, expression=expression, label=m.get("label") or name, table=table,
                                   filters=filters, time_column=time_column, description=m.get("description"),
                                   certified=False)
        elif kind == "ratio":
            num, den = params.get("numerator"), params.get("denominator")
            num = num.get("name") if isinstance(num, dict) else num
            den = den.get("name") if isinstance(den, dict) else den
            metrics[name] = Metric(id=name, expression=f"{{{num}}} / {{{den}}}", label=m.get("label") or name,
                                   description=m.get("description"), certified=False)
        else:
            warnings.append(f"métrique {name} : type {kind} non pris en charge")
    layer = SemanticLayer(domain=domain, entities=entities, metrics=metrics, dimensions=dimensions)
    return ImportReport(layer, warnings)


def merge_layers(base: SemanticLayer, incoming: SemanticLayer, *, source: str = "") -> tuple[SemanticLayer, list[Conflict]]:
    """Ajoute à base ce qui manque ; une définition différente d'un même identifiant devient un conflit."""
    conflicts: list[Conflict] = []
    metrics = dict(base.metrics)
    for mid, metric in incoming.metrics.items():
        current = metrics.get(mid)
        if current is None:
            metrics[mid] = metric
        elif (current.expression, sorted(current.filters)) != (metric.expression, sorted(metric.filters)):
            conflicts.append(Conflict("metric", mid, f"{current.expression} | {current.filters}",
                                      f"{metric.expression} | {metric.filters}", source))
    dimensions = dict(base.dimensions)
    for did, dim in incoming.dimensions.items():
        current = dimensions.get(did)
        if current is None:
            dimensions[did] = dim
        elif current.column.lower() != dim.column.lower():
            conflicts.append(Conflict("dimension", did, current.column, dim.column, source))
    entities = {**incoming.entities, **base.entities}
    merged = msgspec.structs.replace(base, metrics=metrics, dimensions=dimensions, entities=entities)
    return merged, conflicts
