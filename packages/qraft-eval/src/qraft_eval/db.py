"""Connexion aux bases et exécution des requêtes, via SQLAlchemy (tout dialecte)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    elapsed_s: float


def make_engine(url: str) -> Engine:
    """Crée un moteur SQLAlchemy. Les bases DuckDB sont ouvertes en lecture seule.

    Pour toute autre base, utilise un compte dont le rôle n'a aucun droit d'écriture.
    """
    connect_args: dict[str, Any] = {}
    if url.startswith("duckdb:"):
        connect_args["read_only"] = True
    return create_engine(url, connect_args=connect_args)


def run_query(engine: Engine, sql: str, *, max_rows: int = 100_000) -> QueryResult:
    """Exécute une requête de lecture et rapatrie au plus max_rows lignes.

    exec_driver_sql évite que SQLAlchemy interprète les « :nom » comme des paramètres
    (par exemple les casts PostgreSQL « ::date »). La connexion est fermée sans commit.
    """
    started = time.perf_counter()
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql)
        columns = list(result.keys())
        rows = [tuple(row) for row in result.fetchmany(max_rows + 1)]
    truncated = len(rows) > max_rows
    return QueryResult(columns, rows[:max_rows], truncated, time.perf_counter() - started)
