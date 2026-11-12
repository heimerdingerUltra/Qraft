"""Exécution gouvernée : connexion aux sources, files par source, limites, résultats.

SQLAlchemy sert de connecteur universel (tout dialecte disposant d'un pilote).
Chaque source a sa propre limite de concurrence pour protéger la production.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine as SAEngine

from .catalog import SQLGLOT_DIALECTS


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool = False
    elapsed_s: float = 0.0
    sql: str = ""

    def to_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]

    def to_arrow(self) -> Any:
        """Table Arrow (nécessite pyarrow) : zéro copie ensuite vers Polars, pandas ou DuckDB."""
        import pyarrow as pa

        return pa.Table.from_pylist(self.to_dicts()) if self.rows else pa.table({c: [] for c in self.columns})


class SourceError(RuntimeError):
    """La base a refusé ou échoué la requête."""


@dataclass
class Source:
    """Une base de données interrogeable en lecture seule."""

    url: str
    max_concurrency: int = 4
    timeout_s: float = 60.0
    name: str = "defaut"
    _engine: SAEngine | None = field(default=None, init=False, repr=False)
    _slots: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._slots = threading.BoundedSemaphore(self.max_concurrency)

    @property
    def engine(self) -> SAEngine:
        if self._engine is None:
            connect_args: dict[str, Any] = {}
            if self.url.startswith("duckdb:"):
                connect_args["read_only"] = True
            self._engine = create_engine(self.url, connect_args=connect_args)
        return self._engine

    @property
    def dialect(self) -> str:
        name = self.engine.dialect.name
        return SQLGLOT_DIALECTS.get(name, name)

    def query(self, sql: str, *, max_rows: int = 10_000) -> Result:
        """Exécute une lecture ; la connexion est fermée sans commit."""
        acquired = self._slots.acquire(timeout=self.timeout_s)
        if not acquired:
            raise SourceError(f"source {self.name} saturée : aucune place libre après {self.timeout_s:.0f} s")
        started = time.perf_counter()
        try:
            with self.engine.connect() as conn:
                result = conn.exec_driver_sql(sql)
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchmany(max_rows + 1)]
        except Exception as error:  # noqa: BLE001 - on remonte une erreur lisible
            message = str(error).strip().splitlines()[0][:500] if str(error).strip() else type(error).__name__
            raise SourceError(message) from error
        finally:
            self._slots.release()
        return Result(columns, rows[:max_rows], len(rows) > max_rows, time.perf_counter() - started, sql)

    def explain(self, sql: str) -> str | None:
        """Plan d'exécution estimé, quand la base sait le donner (sert de garde-fou de coût)."""
        try:
            with self.engine.connect() as conn:
                rows = conn.exec_driver_sql(f"EXPLAIN {sql}").fetchall()
        except Exception:  # noqa: BLE001 - EXPLAIN n'est pas portable
            return None
        return "\n".join(" ".join(str(c) for c in r) for r in rows)
