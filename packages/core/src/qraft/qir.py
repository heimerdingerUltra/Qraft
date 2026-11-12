"""QIR : la représentation intermédiaire d'une question (intention typée).

Le modèle (ou le parseur à règles) produit un Intent ; le compilateur le transforme
en SQL. Les références pointent vers le graphe de connaissance :
- métrique : identifiant du glossaire (ex. « ca ») ;
- dimension : identifiant du glossaire (ex. « region ») ou colonne « table.colonne » ;
- « time » comme colonne de dimension : la colonne temporelle de la mesure.
"""

from __future__ import annotations

from typing import Any, Literal

import msgspec

Agg = Literal["sum", "count", "count_distinct", "avg", "min", "max"]
Grain = Literal["day", "week", "month", "quarter", "year"]
Op = Literal["=", "!=", "<", "<=", ">", ">=", "in", "not_in", "between", "contains", "is_null", "not_null"]
Compare = Literal["previous_year", "previous_period"]

GRAIN_ALIASES: dict[str, str] = {
    "day": "jour",
    "week": "semaine",
    "month": "mois",
    "quarter": "trimestre",
    "year": "annee",
}


class Filter(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    column: str
    op: Op = "="
    value: Any = None


class Measure(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Une mesure : une métrique du glossaire, ou un agrégat sur une colonne.

    Pour compter les lignes d'une table : agg="count", column="<table>".
    """

    metric: str | None = None
    agg: Agg | None = None
    column: str | None = None
    filters: tuple[Filter, ...] = ()
    alias: str | None = None


class Ratio(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    numerator: Measure
    denominator: Measure
    percent: bool = True
    alias: str | None = None


class Dimension(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    column: str
    grain: Grain | None = None
    alias: str | None = None


class TimeScope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Période [start, end[ en dates ISO. compare ajoute la période de comparaison."""

    start: str | None = None
    end: str | None = None
    column: str | None = None
    compare: Compare | None = None
    compare_output: Literal["all", "pct"] = "all"
    label: str | None = None


class Ranking(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    by: str | None = None
    direction: Literal["desc", "asc"] = "desc"
    limit: int | None = None


class Intent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    measures: tuple[Measure, ...] = ()
    ratios: tuple[Ratio, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    filters: tuple[Filter, ...] = ()
    time: TimeScope | None = None
    ranking: Ranking | None = None
    assumptions: tuple[str, ...] = ()
    escape_sql: str | None = None
    needs_context: tuple[str, ...] = ()
    answerable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return msgspec.to_builtins(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Intent:
        return msgspec.convert(data, cls)

    def fingerprint(self) -> str:
        import hashlib

        return hashlib.sha1(msgspec.json.encode(self)).hexdigest()[:16]
