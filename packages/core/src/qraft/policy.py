"""Politiques de sécurité appliquées par le compilateur, jamais par le prompt.

Elles complètent les droits natifs de la base (identité propagée, RLS native),
qui restent la première ligne de défense.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class UnsafeSQL(ValueError):
    """La requête n'est pas une lecture simple et sûre."""


class PolicyViolation(PermissionError):
    """La requête enfreint une politique (colonne interdite, SQL libre non autorisé...)."""


@dataclass(frozen=True)
class Identity:
    """L'utilisateur au nom duquel la question est posée."""

    user: str = "anonyme"
    attributes: dict[str, str] = field(default_factory=dict)
    roles: tuple[str, ...] = ()


@dataclass
class Policy:
    # Filtres de lignes par table, avec des attributs de l'identité : {"commandes": "commandes.region_id = {region_id}"}
    row_filters: dict[str, str] = field(default_factory=dict)
    # Colonnes interdites, « table.colonne ».
    denied_columns: set[str] = field(default_factory=set)
    # Tables dont les données ne doivent jamais partir vers un modèle cloud.
    restricted_tables: set[str] = field(default_factory=set)
    max_rows: int = 10_000
    allow_free_sql: bool = True

    def denies(self, table: str, column: str) -> bool:
        return f"{table}.{column}".lower() in {c.lower() for c in self.denied_columns}


_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "AlterTable",
    "TruncateTable", "Command", "Copy", "Into", "Pragma", "Use", "Grant", "Revoke",
)
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))
_FORBIDDEN_FUNCTIONS = {
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export",
    "dblink", "dblink_exec", "xp_cmdshell", "load_file", "sleep", "benchmark",
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_ndjson", "read_text", "read_blob", "glob", "nextval", "setval",
}
_QUERY_TYPES = (exp.Query,) if hasattr(exp, "Query") else (exp.Select, exp.Union, exp.Intersect, exp.Except)


def _function_name(node: exp.Func) -> str:
    return str(node.name).lower() if isinstance(node, exp.Anonymous) else node.sql_name().lower()


def check_read_only(sql: str, dialect: str) -> exp.Expression:
    """Parse la requête et vérifie qu'il s'agit d'une seule lecture sûre. Retourne l'AST."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as error:
        raise UnsafeSQL(f"SQL illisible pour le dialecte {dialect} : {error}") from error
    if len(statements) != 1:
        raise UnsafeSQL(f"une seule instruction est autorisée ({len(statements)} trouvées)")
    return check_tree(statements[0])


def check_tree(tree: exp.Expression) -> exp.Expression:
    if not isinstance(tree, _QUERY_TYPES):
        raise UnsafeSQL(f"seules les requêtes de lecture sont autorisées (trouvé : {tree.key.upper()})")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeSQL(f"construction interdite : {node.key.upper()}")
        if isinstance(node, exp.Func) and _function_name(node) in _FORBIDDEN_FUNCTIONS:
            raise UnsafeSQL(f"fonction interdite : {_function_name(node)}")
    return tree


def check_columns(tree: exp.Expression, policy: Policy) -> None:
    """Refuse toute référence à une colonne interdite."""
    if not policy.denied_columns:
        return
    for col in tree.find_all(exp.Column):
        if col.table and policy.denies(col.table, col.name):
            raise PolicyViolation(f"colonne interdite : {col.table}.{col.name}")
        if not col.table and any(d.lower().endswith("." + col.name.lower()) for d in policy.denied_columns):
            raise PolicyViolation(f"colonne interdite : {col.name}")


def row_filter(template: str, identity: Identity, dialect: str) -> exp.Expression:
    """Rend un filtre de lignes : les {attribut} deviennent des littéraux échappés."""
    values = {k: "'" + str(v).replace("'", "''") + "'" for k, v in identity.attributes.items()}
    try:
        rendered = template.format(**values)
    except KeyError as missing:
        raise PolicyViolation(f"attribut d'identité manquant pour un filtre de lignes : {missing}") from missing
    return sqlglot.parse_one(rendered, read=dialect)
