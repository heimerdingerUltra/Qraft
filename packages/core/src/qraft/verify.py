"""Vérification : comparaison de résultats, bases distinctives, contrôles de vraisemblance.

Une requête qui s'exécute n'est pas une requête juste. Les bases distinctives sont de
petites bases synthétiques construites pour faire diverger deux requêtes candidates :
si elles divergent, l'écart devient une question de clarification illustrée.
"""

from __future__ import annotations

import math
import random
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import sqlglot

from .catalog import Table
from .compiler import Compiled
from .execute import Result
from .graph import KnowledgeGraph

# --- Comparaison de résultats ---------------------------------------------------------------


def normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "inf" if number > 0 else "-inf"
        return float(f"{number:.10g}")
    if isinstance(value, datetime):
        return value.date().isoformat() if value.tzinfo is None and value.time() == time(0) else value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value if isinstance(value, str) else str(value)


def same_results(a: Sequence[Sequence[Any]], b: Sequence[Sequence[Any]], *, ordered: bool = False) -> bool:
    na = [tuple(normalize_value(v) for v in r) for r in a]
    nb = [tuple(normalize_value(v) for v in r) for r in b]
    return na == nb if ordered else Counter(na) == Counter(nb)


# --- Bases distinctives -----------------------------------------------------------------------


@dataclass
class Counterexample:
    """Une petite base sur laquelle deux requêtes donnent des résultats différents."""

    tables: dict[str, list[dict[str, Any]]]
    result_a: list[tuple[Any, ...]]
    result_b: list[tuple[Any, ...]]

    def describe(self, limit: int = 3) -> str:
        lines = []
        for name, rows in self.tables.items():
            if rows:
                lines.append(f"{name} : " + " ; ".join(str(r) for r in rows[:limit]))
        return "\n".join(lines)


_DUCK_TYPES = {"text": "VARCHAR", "int": "BIGINT", "num": "DOUBLE", "date": "DATE", "bool": "BOOLEAN"}


def _kind(col: Any) -> str:
    upper = col.type.upper()
    if col.is_temporal:
        return "date"
    if "BOOL" in upper:
        return "bool"
    if col.is_numeric:
        return "int" if "INT" in upper and "POINT" not in upper else "num"
    return "text"


class DistinguishingDB:
    """Génère des bases DuckDB en mémoire respectant le schéma (clés primaires, clés étrangères)
    avec les cas qui piègent : NULL, doublons, orphelins, dates aux bornes."""

    def __init__(self, graph: KnowledgeGraph, *, seed: int = 7) -> None:
        self.graph = graph
        self.rng = random.Random(seed)

    def _value(self, col: Any, kind: str, scope: tuple[date, date]) -> Any:
        r = self.rng
        if col.nullable and r.random() < 0.12:
            return None
        if kind == "text":
            pool = col.values or [f"v{i}" for i in range(4)]
            return r.choice(pool[:6])
        if kind == "int":
            return r.randint(0, 5)
        if kind == "num":
            return round(r.choice([0, 1, 2.5, 10, 99.99, -3]), 2)
        if kind == "bool":
            return r.random() < 0.5
        start, end = scope
        span = max((end - start).days, 1)
        return start + timedelta(days=r.choice([0, span - 1, span, r.randrange(span), -1]))

    def instance(self, tables: list[Table], scope: tuple[date, date]) -> dict[str, list[dict[str, Any]]]:
        data: dict[str, list[dict[str, Any]]] = {}
        order = self._topological(tables)
        for tbl in order:
            n = self.rng.randint(2, 6)
            rows = []
            for i in range(n):
                row = {}
                for col in tbl.columns:
                    kind = _kind(col)
                    if col.name in tbl.primary_key and len(tbl.primary_key) == 1:
                        row[col.name] = i + 1 if kind in ("int", "num") else f"k{i + 1}"
                        continue
                    fk = next((f for f in tbl.foreign_keys if f.column == col.name), None)
                    if fk and fk.ref_table in data and data[fk.ref_table] and self.rng.random() > 0.1:
                        row[col.name] = self.rng.choice(data[fk.ref_table])[fk.ref_column]
                    else:
                        row[col.name] = self._value(col, kind, scope)
                rows.append(row)
            if len(tbl.primary_key) > 1:  # clé composée : on dédoublonne
                seen, unique = set(), []
                for row in rows:
                    key = tuple(row[k] for k in tbl.primary_key)
                    if key not in seen and None not in key:
                        seen.add(key)
                        unique.append(row)
                rows = unique
            data[tbl.name] = rows
        return data

    @staticmethod
    def _topological(tables: list[Table]) -> list[Table]:
        names = {t.name for t in tables}
        ordered, done = [], set()
        pending = list(tables)
        while pending:
            progressed = False
            for tbl in list(pending):
                deps = {f.ref_table for f in tbl.foreign_keys if f.ref_table in names and f.ref_table != tbl.name}
                if deps <= done:
                    ordered.append(tbl)
                    done.add(tbl.name)
                    pending.remove(tbl)
                    progressed = True
            if not progressed:  # cycle : on garde l'ordre restant
                ordered += pending
                break
        return ordered

    def _load(self, con: Any, tables: list[Table], data: dict[str, list[dict[str, Any]]]) -> None:
        for tbl in tables:
            cols = ", ".join(f'"{c.name}" {_DUCK_TYPES[_kind(c)]}' for c in tbl.columns)
            con.execute(f'CREATE TABLE "{tbl.name}" ({cols})')
            rows = data.get(tbl.name, [])
            if rows:
                placeholders = ", ".join("?" for _ in tbl.columns)
                con.executemany(f'INSERT INTO "{tbl.name}" VALUES ({placeholders})',
                                [[row.get(c.name) for c in tbl.columns] for row in rows])

    def find(self, sql_a: str, sql_b: str, table_names: list[str], *, dialect: str,
             scope: tuple[date, date] | None = None, trials: int = 40) -> Counterexample | None:
        """Cherche une base où sql_a et sql_b divergent. None si elles concordent partout."""
        import duckdb

        tables = [self.graph.table(n) for n in table_names]
        a = sqlglot.transpile(sql_a, read=dialect, write="duckdb")[0]
        b = sqlglot.transpile(sql_b, read=dialect, write="duckdb")[0]
        window = scope or (date(2025, 1, 1), date(2026, 1, 1))
        for _ in range(trials):
            data = self.instance(tables, window)
            con = duckdb.connect(":memory:")
            try:
                self._load(con, tables, data)
                ra, rb = con.execute(a).fetchall(), con.execute(b).fetchall()
            except Exception:  # noqa: BLE001 - une instance invalide n'est pas concluante
                continue
            finally:
                con.close()
            if not same_results(ra, rb):
                return Counterexample(data, ra, rb)
        return None


# --- Contrôles de vraisemblance ---------------------------------------------------------------


@dataclass
class Checks:
    warnings: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)


def plausibility(result: Result, compiled: Compiled, graph: KnowledgeGraph, scope_end: str | None = None) -> Checks:
    checks = Checks()
    measure_cols = list(range(int(compiled.signals.get("dims", 0)), len(result.columns)))
    if not result.rows:
        checks.warnings.append("résultat vide : vérifier la période et les filtres")
        checks.flags["empty"] = True
    else:
        for i in measure_cols:
            values = [r[i] for r in result.rows]
            nulls = sum(v is None for v in values)
            if values and nulls / len(values) > 0.5:
                checks.warnings.append(f"plus de la moitié des valeurs de « {result.columns[i]} » sont vides")
                checks.flags["nulls"] = True
    if result.truncated:
        checks.warnings.append("résultat tronqué : seules les premières lignes sont affichées")
        checks.flags["truncated"] = True
    if compiled.signals.get("fanout"):
        checks.warnings.append("risque de double comptage : une dimension est plus fine que la mesure")
    if scope_end:
        # Seules les colonnes temporelles qui filtrent réellement la période comptent.
        for tc in compiled.signals.get("time_columns", []):
            name = tc.split(".")[0]
            _, col = graph.column(tc)
            if col.max and col.max[:10] < (date.fromisoformat(scope_end) - timedelta(days=1)).isoformat() \
                    and col.max[:10] >= (date.fromisoformat(scope_end) - timedelta(days=400)).isoformat():
                checks.warnings.append(f"données de {name} disponibles jusqu'au {col.max[:10]} seulement")
                checks.flags["stale"] = True
                break
    return checks
