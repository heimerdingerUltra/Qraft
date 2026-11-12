"""Couche sémantique : le vocabulaire métier que le schéma ne dit pas.

Fichier YAML versionné, écrit et validé par les stewards :
métriques certifiées, dimensions, synonymes, alias de valeurs, règles globales,
relations manquantes et colonnes temporelles des entités.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import msgspec
import yaml


class Metric(msgspec.Struct, forbid_unknown_fields=True):
    id: str
    expression: str  # agrégat SQL, ou formule de métriques « {ca} / {nb_commandes} »
    label: str | None = None
    table: str | None = None  # table qui porte le grain de la mesure
    filters: list[str] = msgspec.field(default_factory=list)
    time_column: str | None = None
    synonyms: list[str] = msgspec.field(default_factory=list)
    unit: str | None = None
    description: str | None = None
    certified: bool = True

    @property
    def is_composite(self) -> bool:
        return "{" in self.expression


class Dimension(msgspec.Struct, forbid_unknown_fields=True):
    id: str
    column: str
    label: str | None = None
    synonyms: list[str] = msgspec.field(default_factory=list)


class Entity(msgspec.Struct, forbid_unknown_fields=True):
    table: str
    label: str | None = None
    synonyms: list[str] = msgspec.field(default_factory=list)
    time_column: str | None = None


class ValueAlias(msgspec.Struct, forbid_unknown_fields=True):
    column: str
    value: str
    synonyms: list[str] = msgspec.field(default_factory=list)


class Rule(msgspec.Struct, forbid_unknown_fields=True):
    table: str
    filter: str
    label: str | None = None


class Relationship(msgspec.Struct, forbid_unknown_fields=True):
    from_: str = msgspec.field(name="from")  # table.colonne côté « plusieurs »
    to: str = ""  # table.colonne côté « un »
    cardinality: Literal["many_to_one", "one_to_one"] = "many_to_one"


class SemanticLayer(msgspec.Struct, forbid_unknown_fields=True):
    domain: str = "defaut"
    dialect: str | None = None  # dialecte des expressions ; par défaut celui de la base
    version: int = 1
    entities: dict[str, Entity] = msgspec.field(default_factory=dict)
    metrics: dict[str, Metric] = msgspec.field(default_factory=dict)
    dimensions: dict[str, Dimension] = msgspec.field(default_factory=dict)
    values: list[ValueAlias] = msgspec.field(default_factory=list)
    rules: list[Rule] = msgspec.field(default_factory=list)
    relationships: list[Relationship] = msgspec.field(default_factory=list)


def _with_ids(section: dict[str, Any] | None, key: str = "id") -> dict[str, Any]:
    return {name: {key: name, **(body or {})} for name, body in (section or {}).items()}


def parse_semantic(data: dict[str, Any]) -> SemanticLayer:
    data = dict(data or {})
    data["metrics"] = _with_ids(data.get("metrics"))
    data["dimensions"] = _with_ids(data.get("dimensions"))
    data["entities"] = _with_ids(data.get("entities"), key="table")
    return msgspec.convert(data, SemanticLayer)


def load_semantic(path: str | Path) -> SemanticLayer:
    """Charge et valide un fichier de couche sémantique YAML."""
    return parse_semantic(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
