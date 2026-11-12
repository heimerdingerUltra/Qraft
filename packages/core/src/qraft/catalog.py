"""Catalogue technique d'une base : tables, colonnes, clés, profils, clés étrangères inférées.

La réflexion SQLAlchemy est utilisée quand elle fonctionne ; sinon on lit
information_schema (et les catalogues natifs de DuckDB).
"""

from __future__ import annotations

import re
from typing import Any

import msgspec
from sqlalchemy import column, func, inspect, literal_column, select, table, text
from sqlalchemy.engine import Engine
from sqlalchemy.types import String

# Nom du dialecte SQLAlchemy -> nom du dialecte SQLGlot.
SQLGLOT_DIALECTS = {
    "postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "sqlite": "sqlite",
    "mssql": "tsql", "oracle": "oracle", "snowflake": "snowflake", "bigquery": "bigquery",
    "duckdb": "duckdb", "trino": "trino", "redshift": "redshift", "databricks": "databricks",
    "clickhouse": "clickhouse", "hive": "hive", "spark": "spark",
}

_NUMERIC = ("INT", "DEC", "NUM", "FLOAT", "DOUBLE", "REAL", "MONEY", "HUGEINT")
_TEMPORAL = ("DATE", "TIME")
_TEXT = ("CHAR", "TEXT", "STRING", "CLOB", "VARCHAR")


def classify_type(type_name: str) -> tuple[bool, bool, bool]:
    """(texte, numérique, temporel) d'après le nom du type."""
    upper = type_name.upper()
    temporal = any(m in upper for m in _TEMPORAL) and "INTERVAL" not in upper
    numeric = not temporal and any(m in upper for m in _NUMERIC)
    textual = not temporal and not numeric and any(m in upper for m in _TEXT)
    return textual, numeric, temporal


class Column(msgspec.Struct):
    name: str
    type: str
    nullable: bool = True
    is_text: bool = False
    is_numeric: bool = False
    is_temporal: bool = False
    comment: str | None = None
    distinct: int | None = None
    nulls: int | None = None
    min: str | None = None
    max: str | None = None
    values: list[str] = msgspec.field(default_factory=list)  # valeurs distinctes des colonnes à faible cardinalité


class ForeignKey(msgspec.Struct):
    column: str
    ref_table: str
    ref_column: str
    origin: str = "declared"  # declared | inferred | semantic
    confidence: float = 1.0


class Table(msgspec.Struct):
    name: str
    kind: str = "TABLE"
    columns: list[Column] = msgspec.field(default_factory=list)
    primary_key: list[str] = msgspec.field(default_factory=list)
    foreign_keys: list[ForeignKey] = msgspec.field(default_factory=list)
    comment: str | None = None
    rows: int | None = None

    def column(self, name: str) -> Column | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)


class Catalog(msgspec.Struct):
    dialect: str
    schema: str | None = None
    tables: dict[str, Table] = msgspec.field(default_factory=dict)

    def table(self, name: str) -> Table | None:
        if name in self.tables:
            return self.tables[name]
        lowered = name.lower()
        return next((t for n, t in self.tables.items() if n.lower() == lowered), None)

    def resolve(self, ref: str) -> tuple[Table, Column] | None:
        """Résout « table.colonne »."""
        if "." not in ref:
            return None
        table_name, column_name = ref.rsplit(".", 1)
        tbl = self.table(table_name)
        col = tbl.column(column_name) if tbl else None
        return (tbl, col) if tbl and col else None


# --- Introspection -----------------------------------------------------------------------------


def _safe(call: Any, default: Any) -> Any:
    try:
        result = call()
    except Exception:  # noqa: BLE001 - réflexion best-effort selon les dialectes
        return default
    return default if result is None else result


def _make_column(name: str, type_name: str, nullable: bool, comment: str | None = None) -> Column:
    textual, numeric, temporal = classify_type(type_name)
    return Column(name=name, type=type_name, nullable=nullable, is_text=textual,
                  is_numeric=numeric, is_temporal=temporal, comment=comment)


def _schema_filter(engine: Engine, schema: str | None) -> tuple[str, dict[str, Any]]:
    if schema:
        return "table_schema = :schema", {"schema": schema}
    if engine.dialect.name in ("duckdb", "postgresql"):
        return "table_schema = current_schema()", {}
    return "table_schema NOT IN ('information_schema', 'pg_catalog', 'sys', 'INFORMATION_SCHEMA')", {}


def _from_information_schema(engine: Engine, schema: str | None) -> dict[str, Table]:
    where, params = _schema_filter(engine, schema)
    with engine.connect() as conn:
        rels = conn.execute(text(f"SELECT table_name, table_type FROM information_schema.tables WHERE {where}"), params).all()
        cols = conn.execute(
            text("SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                 f"WHERE {where} ORDER BY table_name, ordinal_position"),
            params,
        ).all()
    tables = {name: Table(name=name, kind="VIEW" if "VIEW" in str(kind).upper() else "TABLE") for name, kind in rels}
    for table_name, col_name, data_type, is_nullable in cols:
        if table_name in tables:
            tables[table_name].columns.append(_make_column(col_name, str(data_type).upper(), str(is_nullable).upper() == "YES"))
    if engine.dialect.name == "duckdb":
        _add_duckdb_constraints(engine, tables)
    return tables


def _add_duckdb_constraints(engine: Engine, tables: dict[str, Table]) -> None:
    query = text(
        "SELECT table_name, constraint_type, constraint_column_names, constraint_text FROM duckdb_constraints() "
        "WHERE schema_name = current_schema() AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')"
    )
    with engine.connect() as conn:
        for table_name, kind, cols, definition in conn.execute(query):
            tbl = tables.get(table_name)
            if tbl is None:
                continue
            if kind == "PRIMARY KEY":
                tbl.primary_key = list(cols)
                continue
            ref_table, ref_cols = definition.split("REFERENCES", 1)[1].strip().split("(", 1)
            for col, ref_col in zip(cols, [c.strip() for c in ref_cols.rstrip(")").split(",")]):
                tbl.foreign_keys.append(ForeignKey(column=col, ref_table=ref_table.strip(), ref_column=ref_col))


def _from_inspector(engine: Engine, schema: str | None) -> dict[str, Table]:
    insp = inspect(engine)
    names = [(n, "TABLE") for n in insp.get_table_names(schema=schema)]
    names += [(n, "VIEW") for n in _safe(lambda: insp.get_view_names(schema=schema), [])]
    tables: dict[str, Table] = {}
    for name, kind in names:
        columns = []
        for c in insp.get_columns(name, schema=schema):
            try:
                type_name = str(c["type"])
            except Exception:  # noqa: BLE001
                type_name = type(c["type"]).__name__.upper()
            col = _make_column(c["name"], type_name, bool(c.get("nullable", True)), c.get("comment"))
            if isinstance(c["type"], String):
                col.is_text, col.is_numeric, col.is_temporal = True, False, False
            columns.append(col)
        fks = []
        for fk in _safe(lambda: insp.get_foreign_keys(name, schema=schema), []):
            for col, ref in zip(fk["constrained_columns"], fk["referred_columns"]):
                fks.append(ForeignKey(column=col, ref_table=fk["referred_table"], ref_column=ref))
        tables[name] = Table(
            name=name, kind=kind, columns=columns, foreign_keys=fks,
            primary_key=list(_safe(lambda: insp.get_pk_constraint(name, schema=schema).get("constrained_columns"), [])),
            comment=_safe(lambda: insp.get_table_comment(name, schema=schema).get("text"), None),
        )
    return tables


def introspect(engine: Engine, *, schema: str | None = None) -> Catalog:
    """Lit la structure de la base."""
    if engine.dialect.name == "duckdb":
        tables = _from_information_schema(engine, schema)
    else:
        try:
            tables = _from_inspector(engine, schema)
        except Exception:  # noqa: BLE001 - réflexion incomplète : repli sur information_schema
            tables = _from_information_schema(engine, schema)
    dialect = SQLGLOT_DIALECTS.get(engine.dialect.name, engine.dialect.name)
    return Catalog(dialect=dialect, schema=schema, tables=dict(sorted(tables.items())))


# --- Profilage ---------------------------------------------------------------------------------


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def profile(engine: Engine, catalog: Catalog, *, max_values: int = 200) -> Catalog:
    """Compte les lignes, les valeurs distinctes, les NULL, les bornes ; relève les valeurs
    des colonnes texte à faible cardinalité (pour l'ancrage des valeurs citées dans les questions)."""
    with engine.connect() as conn:
        for tbl in catalog.tables.values():
            if tbl.kind != "TABLE" or not tbl.columns:
                continue
            target = table(tbl.name, *[column(c.name) for c in tbl.columns], schema=catalog.schema)
            aggregates: list[Any] = [func.count().label("__rows")]
            for i, col in enumerate(tbl.columns):
                c = target.c[col.name]
                aggregates += [func.count(c.distinct()).label(f"d{i}"), func.count(c).label(f"n{i}")]
                if col.is_numeric or col.is_temporal:
                    aggregates += [func.min(c).label(f"lo{i}"), func.max(c).label(f"hi{i}")]
            row = conn.execute(select(*aggregates).select_from(target)).mappings().one()
            tbl.rows = int(row["__rows"])
            for i, col in enumerate(tbl.columns):
                col.distinct = int(row[f"d{i}"])
                col.nulls = tbl.rows - int(row[f"n{i}"])
                if col.is_numeric or col.is_temporal:
                    col.min, col.max = _stringify(row[f"lo{i}"]), _stringify(row[f"hi{i}"])
                if col.is_text and 0 < col.distinct <= max_values:
                    c = target.c[col.name]
                    stmt = select(c).where(c.is_not(None)).group_by(c).order_by(func.count().desc(), c).limit(max_values)
                    col.values = [str(r[0]) for r in conn.execute(stmt)]
    return catalog


# --- Clés étrangères inférées ------------------------------------------------------------------

_ID_SUFFIX = re.compile(r"^(?P<stem>.+?)_?(id|code|key|num)$", re.IGNORECASE)


def key_column(table: Table) -> Column | None:
    """Clé d'une table : sa clé primaire mono-colonne, sinon une colonne « id » unique et non nulle (profilée).

    Les entrepôts de données déclarent rarement leurs clés : l'unicité observée en tient lieu.
    """
    if len(table.primary_key) == 1:
        return table.column(table.primary_key[0])
    if table.rows is None:
        return None
    stems = {table.name.lower(), table.name.lower().rstrip("s"), table.name.lower().rstrip("x")}
    for col in table.columns:
        match = _ID_SUFFIX.match(col.name)
        named_like_key = col.name.lower() == "id" or (match and match.group("stem").lower().rstrip("_") in stems)
        if named_like_key and col.distinct == table.rows and col.nulls == 0 and table.rows > 0:
            return col
    return None


def _candidate_targets(col: Column, owner: Table, catalog: Catalog) -> list[tuple[Table, Column]]:
    """Tables dont la clé (déclarée ou observée) porte un nom compatible avec col."""
    candidates = []
    match = _ID_SUFFIX.match(col.name)
    stem = (match.group("stem") if match else col.name).lower().rstrip("_")
    for target in catalog.tables.values():
        if target.name == owner.name:
            continue
        pk = key_column(target)
        if pk is None or pk.is_text != col.is_text:
            continue
        names = {target.name.lower(), target.name.lower().rstrip("s"), target.name.lower().rstrip("x")}
        if pk.name.lower() == col.name.lower() or stem in names:
            candidates.append((target, pk))
    return candidates


def infer_foreign_keys(engine: Engine, catalog: Catalog, *, min_inclusion: float = 0.98) -> list[ForeignKey]:
    """Propose des clés étrangères non déclarées, vérifiées par inclusion des valeurs.

    Une colonne est retenue si au moins min_inclusion de ses valeurs non nulles existent
    dans la clé primaire de la table visée.
    """
    inferred: list[ForeignKey] = []
    with engine.connect() as conn:
        for owner in catalog.tables.values():
            declared = {fk.column.lower() for fk in owner.foreign_keys}
            own_key = key_column(owner)
            for col in owner.columns:
                if col.name.lower() in declared or col.name in owner.primary_key or col.is_temporal:
                    continue
                if own_key is not None and col.name == own_key.name:
                    continue
                for target, pk in _candidate_targets(col, owner, catalog):
                    child = table(owner.name, column(col.name), schema=catalog.schema).alias("enfant")
                    parent = table(target.name, column(pk.name), schema=catalog.schema).alias("parent")
                    joined = child.outerjoin(parent, child.c[col.name] == parent.c[pk.name])
                    stmt = select(
                        func.count(child.c[col.name]).label("non_null"),
                        func.count(literal_column("1")).filter(
                            child.c[col.name].is_not(None) & parent.c[pk.name].is_(None)
                        ).label("orphans"),
                    ).select_from(joined)
                    try:
                        non_null, orphans = conn.execute(stmt).one()
                    except Exception:  # noqa: BLE001 - types incompatibles, etc.
                        continue
                    if non_null and (non_null - orphans) / non_null >= min_inclusion:
                        inferred.append(ForeignKey(column=col.name, ref_table=target.name, ref_column=pk.name,
                                                   origin="inferred", confidence=round((non_null - orphans) / non_null, 4)))
                        owner.foreign_keys.append(inferred[-1])
                        break
    return inferred
