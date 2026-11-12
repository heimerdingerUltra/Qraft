"""Fédération : la base de démo coupée en deux (CRM / facturation) donne les mêmes réponses."""

import json

import duckdb
import msgspec
import pytest

from qraft.federation import federated_engine
from qraft.importers import import_dbt_manifest, merge_layers
from qraft.semantic import Relationship, load_semantic
from qraft.verify import same_results

from .conftest import SEMANTIC


@pytest.fixture(scope="module")
def split_urls(demo_url, tmp_path_factory):
    src = demo_url.replace("duckdb:///", "")
    folder = tmp_path_factory.mktemp("split")
    parts = {"crm": ["regions", "clients"], "facturation": ["produits", "commandes", "lignes_commande", "paiements"]}
    urls = {}
    for name, tables in parts.items():
        path = folder / f"{name}.duckdb"
        con = duckdb.connect(str(path))
        con.execute(f"ATTACH '{src}' AS demo (READ_ONLY)")
        for t in tables:
            con.execute(f"CREATE TABLE {t} AS SELECT * FROM demo.{t}")
        con.close()
        urls[name] = f"duckdb:///{path.as_posix()}"
    return urls


def test_federated_answer_matches_single_database(engine, split_urls):
    layer = load_semantic(SEMANTIC)
    layer = msgspec.structs.replace(layer, relationships=[
        Relationship(from_="commandes.client_id", to="clients.client_id")])
    fed = federated_engine(split_urls, semantic=layer)
    question = "Chiffre d'affaires 2025 par région"
    federated, single = fed.ask(question), engine.ask(question)
    assert federated.status in ("ok", "a_confirmer"), federated.message
    assert same_results(federated.rows, single.rows)
    # Clés non déclarées (CREATE TABLE AS) : les relations internes à chaque base sont inférées.
    assert any(e.origin == "inferred" for e in fed.graph.edges)


def test_dbt_import_and_conflicts(tmp_path):
    manifest = {
        "nodes": {"model.p.commandes": {"resource_type": "model", "name": "commandes", "description": "Commandes"}},
        "semantic_models": {"semantic_model.p.lignes": {
            "name": "lignes", "node_relation": {"alias": "lignes_commande"},
            "defaults": {"agg_time_dimension": "date_commande"},
            "dimensions": [{"name": "date_commande", "type": "time", "expr": "commandes.date_commande"},
                           {"name": "canal", "type": "categorical", "expr": "commandes.canal"}],
            "measures": [{"name": "montant", "agg": "sum", "expr": "quantite * prix_unitaire"}]}},
        "metrics": {
            "metric.p.ca": {"name": "ca", "type": "simple", "label": "CA",
                            "type_params": {"measure": {"name": "montant"}},
                            "filter": {"where_filters": [{"where_sql_template": "{{ Dimension('commande__statut') }} = 'payee'"}]}},
            "metric.p.ca_brut": {"name": "ca_brut", "type": "simple", "type_params": {"measure": "montant"}},
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = import_dbt_manifest(path)
    assert set(report.layer.metrics) == {"ca", "ca_brut"}
    assert report.layer.metrics["ca"].filters == ["lignes_commande.statut = 'payee'"]
    merged, conflicts = merge_layers(load_semantic(SEMANTIC), report.layer, source="dbt")
    assert "ca_brut" in merged.metrics
    assert [c.id for c in conflicts if c.kind == "metric"] == ["ca"]
