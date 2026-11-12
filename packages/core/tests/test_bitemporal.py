"""Définitions métier datées : une question sur une période prend la définition en vigueur à cette période."""

import msgspec

from qraft import Compiler, Intent, KnowledgeGraph, Measure, TimeScope
from qraft.semantic import MetricVersion


def _graph_with_versioned_ca(graph):
    ca = graph.semantic.metrics["ca"]
    versioned = msgspec.structs.replace(ca, versions=[MetricVersion(
        valid_from="2025-07-01", filters=["commandes.statut IN ('payee', 'en_attente')"],
        known_since="2025-06-15", note="les commandes en attente comptent désormais")])
    layer = msgspec.structs.replace(graph.semantic, metrics={**graph.semantic.metrics, "ca": versioned})
    return KnowledgeGraph(graph.catalog, layer)


def test_definition_follows_the_business_period(graph):
    g = _graph_with_versioned_ca(graph)
    h1 = Compiler(g).compile(Intent(measures=(Measure(metric="ca"),), time=TimeScope(start="2025-01-01", end="2025-07-01")))
    h2 = Compiler(g).compile(Intent(measures=(Measure(metric="ca"),), time=TimeScope(start="2025-07-01", end="2026-01-01")))
    assert "en_attente" not in h1.sql and "en_attente" in h2.sql
    assert any("en vigueur depuis le 2025-07-01" in r for r in h2.rules)


def test_knowledge_time_makes_past_answers_reproducible(graph):
    g = _graph_with_versioned_ca(graph)
    intent = Intent(measures=(Measure(metric="ca"),), time=TimeScope(start="2025-07-01", end="2026-01-01"))
    as_known_before = Compiler(g, known_at="2025-06-01").compile(intent)
    assert "en_attente" not in as_known_before.sql


def test_comparison_uses_each_period_definition(graph):
    g = _graph_with_versioned_ca(graph)
    intent = Intent(measures=(Measure(metric="ca"),),
                    time=TimeScope(start="2025-07-01", end="2026-01-01", compare="previous_year"))
    sql = Compiler(g).compile(intent).sql
    current, previous = sql.split("precedent AS", 1)
    assert "en_attente" in current and "en_attente" not in previous
