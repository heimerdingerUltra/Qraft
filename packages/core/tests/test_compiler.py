import pytest
import sqlglot

from qraft import Compiler, Dimension, Filter, Identity, Intent, Measure, Policy, PolicyViolation, Ranking, Ratio, TimeScope
from qraft.graph import CompileError

Y2025 = TimeScope(start="2025-01-01", end="2026-01-01")


def run(engine, intent, **kw):
    compiled = Compiler(engine.graph, **kw).compile(intent)
    return compiled, engine.source.query(compiled.sql).rows


def test_metric_filters_and_joins_come_from_the_graph(engine):
    compiled, rows = run(engine, Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="region"),), time=Y2025))
    assert "commandes.statut = 'payee'" in compiled.sql
    assert compiled.sql.count("JOIN") == 3  # lignes -> commandes -> clients -> regions
    assert len(rows) == 6


def test_fan_trap_uses_one_group_per_grain(engine):
    compiled, rows = run(engine, Intent(measures=(Measure(metric="ca"), Measure(metric="encaisse")),
                                        dimensions=(Dimension(column="segment"),), time=Y2025))
    assert compiled.signals["groups"] == 2
    assert "FULL" in compiled.sql.upper()
    # Sans pré-agrégation, joindre lignes et paiements doublerait les montants : on vérifie la valeur exacte.
    ca_only = run(engine, Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="segment"),), time=Y2025))[1]
    assert sorted(r[:2] for r in rows) == sorted(ca_only)


def test_filter_on_child_table_becomes_semi_join(engine):
    compiled, rows = run(engine, Intent(measures=(Measure(agg="count", column="clients"),),
                                        filters=(Filter(column="produits.categorie", value="Mobilier"),)))
    assert "EXISTS" in compiled.sql.upper()
    assert not compiled.signals["fanout"]
    total = engine.source.query("SELECT COUNT(*) FROM clients").rows[0][0]
    assert 0 < rows[0][0] <= total


def test_composite_metric_and_ratio(engine):
    _, rows = run(engine, Intent(measures=(Measure(metric="panier_moyen"),), time=Y2025))
    ca = engine.source.query(Compiler(engine.graph).compile(Intent(measures=(Measure(metric="ca"),), time=Y2025)).sql).rows[0][0]
    n = engine.source.query(Compiler(engine.graph).compile(Intent(measures=(Measure(metric="nb_commandes"),), time=Y2025)).sql).rows[0][0]
    assert rows[0][0] == pytest.approx(float(ca) / n)
    compiled, rows = run(engine, Intent(ratios=(Ratio(numerator=Measure(metric="ca", filters=(Filter(column="canal", value="web"),)),
                                                       denominator=Measure(metric="ca")),),
                                        dimensions=(Dimension(column="time", grain="month"),), time=Y2025))
    assert compiled.columns == ["mois", "part_ca_web_pct"]
    assert len(rows) == 12 and all(0 <= r[1] <= 100 for r in rows)


def test_previous_year_comparison(engine):
    compiled, rows = run(engine, Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="categorie"),),
                                        time=TimeScope(start="2025-01-01", end="2026-01-01", compare="previous_year")))
    assert compiled.columns == ["categorie", "ca", "ca_precedent", "ca_evolution_pct"]
    assert len(rows) == 4


def test_ranking_orders_and_limits(engine):
    compiled, rows = run(engine, Intent(measures=(Measure(metric="quantite_vendue"),), dimensions=(Dimension(column="produit"),),
                                        time=Y2025, ranking=Ranking(limit=3)))
    assert len(rows) == 3 and rows[0][1] >= rows[1][1] >= rows[2][1]


@pytest.mark.parametrize("dialect", ["postgres", "snowflake", "tsql", "bigquery", "mysql", "oracle"])
def test_transpiles_to_other_dialects(graph, dialect):
    compiled = Compiler(graph).compile(Intent(measures=(Measure(metric="ca"), Measure(metric="encaisse")),
                                              dimensions=(Dimension(column="time", grain="month"),), time=Y2025))
    sql = compiled.ast.sql(dialect=dialect)
    assert sqlglot.parse_one(sql, read=dialect) is not None


def test_denied_column_is_refused(graph):
    policy = Policy(denied_columns={"clients.nom"})
    with pytest.raises(PolicyViolation):
        Compiler(graph, policy=policy).compile(Intent(dimensions=(Dimension(column="client"),)))


def test_row_filter_is_injected(engine):
    policy = Policy(row_filters={"regions": "regions.code = {region}"})
    compiled, rows = run(engine, Intent(measures=(Measure(agg="count", column="clients"),),
                                        dimensions=(Dimension(column="region"),)),
                         policy=policy, identity=Identity(user="u", attributes={"region": "IDF"}))
    assert "regions.code = 'IDF'" in compiled.sql
    assert [r[0] for r in rows] == ["Île-de-France"]


def test_unknown_reference_is_a_compile_error(graph):
    with pytest.raises(CompileError):
        Compiler(graph).compile(Intent(measures=(Measure(metric="marge"),)))


def test_free_sql_is_checked(graph):
    with pytest.raises(Exception):
        Compiler(graph).compile(Intent(escape_sql="DELETE FROM clients"))
    compiled = Compiler(graph).compile(Intent(escape_sql="SELECT COUNT(*) AS n FROM clients"))
    assert compiled.signals["escape"]
