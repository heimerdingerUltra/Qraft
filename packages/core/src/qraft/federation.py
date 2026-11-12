"""Fédération multi-sources (v0) : une question qui touche plusieurs bases.

Chaque table nécessaire est lue dans sa base d'origine (avec un plafond de lignes),
puis la requête compilée en dialecte DuckDB s'exécute en mémoire, dans le processus.
Le plafond protège la mémoire : au-delà, la fédération refuse plutôt que de saturer.
Les relations entre bases se déclarent dans la couche sémantique (relationships).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from .catalog import Catalog, infer_foreign_keys, introspect, profile
from .execute import Result, Source, SourceError
from .graph import KnowledgeGraph
from .semantic import SemanticLayer, load_semantic


@dataclass
class FederatedSource:
    sources: dict[str, Source]
    table_map: dict[str, str]  # table -> nom de la source
    max_rows_per_table: int = 1_000_000
    name: str = "federation"
    dialect: str = field(default="duckdb", init=False)

    def _referenced_tables(self, sql: str) -> list[str]:
        tree = sqlglot.parse_one(sql, read="duckdb")
        ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        return sorted({t.name for t in tree.find_all(exp.Table) if t.name not in ctes})

    def query(self, sql: str, *, max_rows: int = 10_000) -> Result:
        import duckdb
        import pyarrow as pa

        started = time.perf_counter()
        con = duckdb.connect(":memory:")
        try:
            for table in self._referenced_tables(sql):
                source_name = self.table_map.get(table)
                if source_name is None:
                    raise SourceError(f"table {table} rattachée à aucune source")
                src = self.sources[source_name]
                quoted = exp.to_table(table).sql(dialect=src.dialect)
                part = src.query(f"SELECT * FROM {quoted}", max_rows=self.max_rows_per_table)
                if part.truncated:
                    raise SourceError(f"table {table} trop volumineuse pour la fédération "
                                      f"(plus de {self.max_rows_per_table} lignes)")
                arrow = pa.Table.from_pylist(part.to_dicts()) if part.rows else pa.table({c: [] for c in part.columns})
                con.register(table, arrow)
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description]
            rows = [tuple(r) for r in cursor.fetchmany(max_rows + 1)]
        except SourceError:
            raise
        except Exception as error:  # noqa: BLE001
            raise SourceError(str(error).strip().splitlines()[0][:500]) from error
        finally:
            con.close()
        return Result(columns, rows[:max_rows], len(rows) > max_rows, time.perf_counter() - started, sql)


def federated_graph(urls: dict[str, str], *, semantic: str | Path | SemanticLayer | None = None,
                    do_profile: bool = True) -> tuple[KnowledgeGraph, FederatedSource]:
    """Introspecte chaque source, fusionne les catalogues (dialecte DuckDB) et rattache chaque table."""
    sources = {name: Source(url, name=name) for name, url in urls.items()}
    tables, table_map = {}, {}
    for name, src in sources.items():
        catalog = introspect(src.engine)
        if do_profile:
            profile(src.engine, catalog)
            infer_foreign_keys(src.engine, catalog)
        for tname, tbl in catalog.tables.items():
            if tname in table_map:
                raise ValueError(f"table {tname} présente dans {table_map[tname]} et {name} : renommer ou préfixer")
            tables[tname], table_map[tname] = tbl, name
    layer = load_semantic(semantic) if isinstance(semantic, (str, Path)) else semantic
    if layer is not None and layer.dialect not in (None, "duckdb"):
        layer = SemanticLayer(**{**{f: getattr(layer, f) for f in layer.__struct_fields__}, "dialect": "duckdb"})
    graph = KnowledgeGraph(Catalog(dialect="duckdb", tables=dict(sorted(tables.items()))), layer)
    return graph, FederatedSource(sources, table_map)


def federated_engine(urls: dict[str, str], *, semantic: str | Path | SemanticLayer | None = None, **kwargs: Any):
    from .engine import Engine

    graph, source = federated_graph(urls, semantic=semantic)
    return Engine(source, graph, **kwargs)  # type: ignore[arg-type]
