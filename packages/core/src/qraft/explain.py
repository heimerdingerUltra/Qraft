"""Rendu déterministe d'une intention en français : fidèle par construction, sans modèle."""

from __future__ import annotations

from datetime import date, timedelta

from .graph import KnowledgeGraph
from .qir import Filter, Intent, Measure, Ratio

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]
AGGREGATES = {"sum": "somme de", "avg": "moyenne de", "min": "minimum de", "max": "maximum de",
              "count": "nombre de", "count_distinct": "nombre distinct de"}
GRAINS = {"day": "jour", "week": "semaine", "month": "mois", "quarter": "trimestre", "year": "année"}
OPS = {"=": "=", "!=": "≠", "<": "<", "<=": "≤", ">": ">", ">=": "≥", "in": "parmi", "not_in": "hors de",
       "between": "entre", "contains": "contient", "is_null": "est vide", "not_null": "est renseigné"}


def french_date(value: str) -> str:
    d = date.fromisoformat(value)
    day = "1er" if d.day == 1 else str(d.day)
    return f"{day} {MONTHS[d.month - 1]} {d.year}"


def describe_period(start: str | None, end: str | None) -> str:
    if start and end:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        if s.month == 1 and s.day == 1 and e == s.replace(year=s.year + 1):
            return f"en {s.year}"
        last = e - timedelta(days=1)
        return f"du {french_date(start)} au {french_date(last.isoformat())}"
    if start:
        return f"depuis le {french_date(start)}"
    if end:
        return f"avant le {french_date(end)}"
    return ""


class Explainer:
    def __init__(self, graph: KnowledgeGraph) -> None:
        self.graph = graph

    def _column_label(self, ref: str) -> str:
        if ref in self.graph.semantic.dimensions:
            return self.graph.label("dimension", ref)
        for dim in self.graph.semantic.dimensions.values():
            if dim.column.lower() == ref.lower():
                return dim.label or dim.id
        return ref.split(".")[-1].replace("_", " ")

    def filter_text(self, f: Filter) -> str:
        label = self._column_label(f.column)
        if f.op in ("is_null", "not_null"):
            return f"{label} {OPS[f.op]}"
        value = ", ".join(map(str, f.value)) if isinstance(f.value, (list, tuple)) else str(f.value)
        return f"{label} {OPS[f.op]} {value}"

    def measure_text(self, m: Measure) -> str:
        if m.metric:
            text = self.graph.label("metric", m.metric)
        elif m.agg == "count" and m.column and "." not in m.column:
            text = f"nombre de {self.graph.label('entity', m.column)}"
        else:
            text = f"{AGGREGATES.get(m.agg or '', '')} {self._column_label(m.column or '')}".strip()
        if m.filters:
            text += " (" + ", ".join(self.filter_text(f) for f in m.filters) + ")"
        return text

    def ratio_text(self, r: Ratio) -> str:
        text = f"part de {self.measure_text(r.numerator)} dans {self.measure_text(r.denominator)}"
        return text + (" (en %)" if r.percent else "")

    def explain(self, intent: Intent) -> str:
        if intent.escape_sql:
            return "Requête en SQL libre (non exprimable en QIR)."
        parts = [self.measure_text(m) for m in intent.measures] + [self.ratio_text(r) for r in intent.ratios]
        head = " et ".join(parts) if parts else "liste de " + ", ".join(
            self._column_label(d.column) for d in intent.dimensions)
        sentence = head[:1].upper() + head[1:]
        dims = [f"par {GRAINS[d.grain]}" if d.column == "time" or d.grain else f"par {self._column_label(d.column)}"
                for d in intent.dimensions] if parts else []
        if dims:
            sentence += " " + ", ".join(dims)
        if intent.filters:
            sentence += ", où " + " et ".join(self.filter_text(f) for f in intent.filters)
        if intent.time and (intent.time.start or intent.time.end):
            sentence += ", " + describe_period(intent.time.start, intent.time.end)
            if intent.time.compare == "previous_year":
                sentence += ", comparé à l'année précédente"
            elif intent.time.compare == "previous_period":
                sentence += ", comparé à la période précédente"
            if intent.time.compare and intent.time.compare_output == "pct":
                sentence += " (évolution en %)"
        if intent.ranking:
            order = "décroissant" if intent.ranking.direction == "desc" else "croissant"
            if intent.ranking.limit:
                sentence += f", {intent.ranking.limit} premiers par ordre {order}"
            else:
                sentence += f", trié par ordre {order}"
        return sentence + "."
