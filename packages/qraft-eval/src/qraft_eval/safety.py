"""Garde-fou de lecture seule : on n'exécute que des requêtes SELECT vérifiées sur l'AST.

Ce contrôle complète, sans le remplacer, un rôle de base en lecture seule :
la phase 0 doit toujours tourner avec un compte sans droit d'écriture.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class UnsafeSQL(ValueError):
    """La requête n'est pas une lecture simple et sûre."""


# Nœuds qui écrivent, modifient le schéma ou sortent du cadre d'une lecture.
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "AlterTable",
    "TruncateTable", "Command", "Copy", "Into", "Pragma", "Use", "Grant", "Revoke",
)
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))

# Fonctions à effet de bord, d'attente ou d'accès aux fichiers.
_FORBIDDEN_FUNCTIONS = {
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export",
    "dblink", "dblink_exec", "xp_cmdshell", "load_file", "sleep", "benchmark",
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_ndjson", "read_text", "read_blob", "glob", "nextval", "setval",
}

_QUERY_TYPES = (exp.Query,) if hasattr(exp, "Query") else (exp.Select, exp.Union, exp.Intersect, exp.Except)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def check_read_only(sql: str, dialect: str) -> exp.Expression:
    """Parse la requête et vérifie qu'il s'agit d'une seule lecture sûre. Retourne l'AST."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as error:
        raise UnsafeSQL(f"SQL illisible pour le dialecte {dialect} : {error}") from error
    if len(statements) != 1:
        raise UnsafeSQL(f"une seule instruction est autorisée ({len(statements)} trouvées)")
    tree = statements[0]
    if not isinstance(tree, _QUERY_TYPES):
        raise UnsafeSQL(f"seules les requêtes de lecture sont autorisées (trouvé : {tree.key.upper()})")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeSQL(f"construction interdite : {node.key.upper()}")
        if isinstance(node, exp.Func) and _function_name(node) in _FORBIDDEN_FUNCTIONS:
            raise UnsafeSQL(f"fonction interdite : {_function_name(node)}")
    return tree


def has_top_level_order_by(tree: exp.Expression) -> bool:
    """True si la requête trie son résultat final (la comparaison doit alors être ordonnée)."""
    return bool(tree.args.get("order"))
