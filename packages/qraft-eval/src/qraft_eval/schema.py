"""Description compacte et déterministe d'un schéma, pour le prompt de la baseline.

Le texte est trié (tables, colonnes) et ne contient rien de variable : il sert de
préfixe stable, donc mis en cache par l'API d'une question à l'autre.

La réflexion SQLAlchemy est utilisée quand elle fonctionne. Sinon (dialecte incomplet,
ou DuckDB dont la réflexion casse avec SQLAlchemy 2.1), on lit information_schema,
qui existe dans la plupart des bases SQL.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import column, func, inspect, select, table, text
from sqlalchemy.engine import Engine, Inspector
from sqlalchemy.types import String

T = TypeVar("T")

_TEXT_TYPE_MARKERS = ("CHAR", "TEXT", "STRING", "CLOB")


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type_name: str
    nullable: bool
    is_text: bool
    comment: str | None = None


@dataclass(frozen=True)
class RelationInfo:
    name: str
    kind: str  # TABLE ou VIEW
    columns: list[ColumnInfo]
    primary_key: set[str]
    foreign_keys: dict[str, str]  # colonne -> table.colonne
    comment: str | None = None


def _safe(call: Callable[[], T], default: T) -> T:
    """Réflexion best-effort : un dialecte incomplet ne doit pas faire échouer la description."""
    try:
        result = call()
    except Exception:  # noqa: BLE001
        return default
    return default if result is None else result


def _type_name(column_type: Any) -> str:
    try:
        return str(column_type)
    except Exception:  # noqa: BLE001 - type exotique non compilable hors de son dialecte
        return type(column_type).__name__.upper()


# --- Lecture via information_schema (et catalogues DuckDB) -----------------------------------


def _schema_filter(engine: Engine, schema: str | None) -> tuple[str, dict[str, Any]]:
    if schema:
        return "table_schema = :schema", {"schema": schema}
    if engine.dialect.name in ("duckdb", "postgresql"):
        return "table_schema = current_schema()", {}
    return "table_schema NOT IN ('information_schema', 'pg_catalog', 'sys', 'INFORMATION_SCHEMA')", {}


def _relations_from_information_schema(engine: Engine, schema: str | None) -> list[RelationInfo]:
    where, params = _schema_filter(engine, schema)
    with engine.connect() as conn:
        relations = conn.execute(
            text(f"SELECT table_name, table_type FROM information_schema.tables WHERE {where}"), params
        ).all()
        column_rows = conn.execute(
            text(
                "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                f"WHERE {where} ORDER BY table_name, ordinal_position"
            ),
            params,
        ).all()
    columns: dict[str, list[ColumnInfo]] = {}
    for table_name, col_name, data_type, is_nullable in column_rows:
        type_name = str(data_type).upper()
        columns.setdefault(table_name, []).append(
            ColumnInfo(col_name, type_name, str(is_nullable).upper() == "YES",
                       any(m in type_name for m in _TEXT_TYPE_MARKERS))
        )
    primary_keys = _duckdb_constraints(engine, "PRIMARY KEY") if engine.dialect.name == "duckdb" else {}
    foreign_keys = _duckdb_foreign_keys(engine) if engine.dialect.name == "duckdb" else {}
    return [
        RelationInfo(
            name=name,
            kind="VIEW" if "VIEW" in str(kind).upper() else "TABLE",
            columns=columns.get(name, []),
            primary_key=set(primary_keys.get(name, [])),
            foreign_keys=foreign_keys.get(name, {}),
        )
        for name, kind in relations
    ]


def _duckdb_constraints(engine: Engine, constraint_type: str) -> dict[str, list[str]]:
    query = text(
        "SELECT table_name, constraint_column_names FROM duckdb_constraints() "
        "WHERE constraint_type = :kind AND schema_name = current_schema()"
    )
    with engine.connect() as conn:
        return {name: list(cols) for name, cols in conn.execute(query, {"kind": constraint_type})}


def _duckdb_foreign_keys(engine: Engine) -> dict[str, dict[str, str]]:
    query = text(
        "SELECT table_name, constraint_column_names, constraint_text FROM duckdb_constraints() "
        "WHERE constraint_type = 'FOREIGN KEY' AND schema_name = current_schema()"
    )
    found: dict[str, dict[str, str]] = {}
    with engine.connect() as conn:
        for table_name, cols, definition in conn.execute(query):
            # constraint_text : FOREIGN KEY (col) REFERENCES autre_table(col_ref)
            ref_table, ref_cols = definition.split("REFERENCES", 1)[1].strip().split("(", 1)
            ref_columns = [c.strip() for c in ref_cols.rstrip(")").split(",")]
            for col, ref_col in zip(cols, ref_columns):
                found.setdefault(table_name, {})[col] = f"{ref_table.strip()}.{ref_col}"
    return found


# --- Lecture via la réflexion SQLAlchemy -----------------------------------------------------


def _relation_from_inspector(insp: Inspector, name: str, kind: str, schema: str | None) -> RelationInfo:
    columns = [
        ColumnInfo(
            name=c["name"],
            type_name=_type_name(c["type"]),
            nullable=bool(c.get("nullable", True)),
            is_text=isinstance(c["type"], String),
            comment=c.get("comment"),
        )
        for c in insp.get_columns(name, schema=schema)
    ]
    foreign_keys: dict[str, str] = {}
    for fk in _safe(lambda: insp.get_foreign_keys(name, schema=schema), []):
        for col, ref in zip(fk["constrained_columns"], fk["referred_columns"]):
            foreign_keys[col] = f"{fk['referred_table']}.{ref}"
    return RelationInfo(
        name=name,
        kind=kind,
        columns=columns,
        primary_key=set(_safe(lambda: insp.get_pk_constraint(name, schema=schema).get("constrained_columns"), [])),
        foreign_keys=foreign_keys,
        comment=_safe(lambda: insp.get_table_comment(name, schema=schema).get("text"), None),
    )


def _relations_from_inspector(engine: Engine, schema: str | None) -> list[RelationInfo]:
    insp = inspect(engine)
    tables = [(n, "TABLE") for n in insp.get_table_names(schema=schema)]
    views = [(n, "VIEW") for n in _safe(lambda: insp.get_view_names(schema=schema), [])]
    return [_relation_from_inspector(insp, name, kind, schema) for name, kind in tables + views]


def list_relations(engine: Engine, *, schema: str | None = None) -> list[RelationInfo]:
    """Tables et vues triées, par réflexion SQLAlchemy ou à défaut par information_schema."""
    if engine.dialect.name == "duckdb":
        relations = _relations_from_information_schema(engine, schema)
    else:
        try:
            relations = _relations_from_inspector(engine, schema)
        except Exception:  # noqa: BLE001 - dialecte dont la réflexion est incomplète
            relations = _relations_from_information_schema(engine, schema)
    return sorted(relations, key=lambda r: (r.kind != "TABLE", r.name))


# --- Rendu ------------------------------------------------------------------------------------


def _top_values(engine: Engine, relation: str, col: str, schema: str | None, limit: int) -> list[Any]:
    """Valeurs les plus fréquentes d'une colonne, sans réflexion (LIMIT compilé par dialecte)."""
    target = column(col)
    stmt = (
        select(target)
        .select_from(table(relation, target, schema=schema))
        .where(target.is_not(None))
        .group_by(target)
        .order_by(func.count().desc(), target)
        .limit(limit)
    )
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(stmt)]


def describe_schema(
    engine: Engine,
    *,
    schema: str | None = None,
    include_samples: bool = False,
    sample_values: int = 5,
) -> str:
    """Rend le schéma (tables puis vues) en texte compact.

    include_samples ajoute les valeurs les plus fréquentes des colonnes texte.
    Désactivé par défaut : ces valeurs partent dans le prompt du modèle.
    """
    blocks: list[str] = []
    for rel in list_relations(engine, schema=schema):
        lines = [f"{rel.kind} {rel.name}" + (f"  -- {rel.comment}" if rel.comment else "")]
        for col in rel.columns:
            parts = [f"  {col.name} {col.type_name}"]
            if col.name in rel.primary_key:
                parts.append("PRIMARY KEY")
            elif not col.nullable:
                parts.append("NOT NULL")
            if col.name in rel.foreign_keys:
                parts.append(f"-> {rel.foreign_keys[col.name]}")
            notes = [col.comment] if col.comment else []
            if include_samples and col.is_text and col.name not in rel.primary_key:
                values = _safe(lambda: _top_values(engine, rel.name, col.name, schema, sample_values), [])
                if values:
                    notes.append("valeurs fréquentes : " + ", ".join(repr(v) for v in values))
            line = " ".join(parts)
            if notes:
                line += "  -- " + " ; ".join(notes)
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
