"""Bout en bout sur la base de démo, avec des générateurs factices (aucun appel au modèle)."""

from pathlib import Path

import pytest

from qraft_eval.baseline import (
    CORRECT,
    INCORRECT,
    RIGHT_ABSTENTION,
    SQL_ERROR,
    SQL_REJECTED,
    WRONG_ANSWER,
    Prediction,
    run_baseline,
)
from qraft_eval.db import make_engine
from qraft_eval.demo import build_demo_db
from qraft_eval.goldset import load_goldset
from qraft_eval.report import qir_coverage, render_markdown, summarize
from qraft_eval.schema import describe_schema
from qraft_eval.validate import ERROR, validate_goldset

GOLDSET = Path(__file__).resolve().parents[3] / "phase0" / "goldsets" / "demo_ventes.yaml"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    path = build_demo_db(tmp_path_factory.mktemp("demo") / "ventes.duckdb")
    return make_engine(f"duckdb:///{path.as_posix()}")


@pytest.fixture(scope="module")
def goldset():
    return load_goldset(GOLDSET)


class OracleGenerator:
    """Renvoie la référence : doit obtenir 100 %."""

    model = "oracle"

    def __init__(self, goldset):
        self.by_question = {q.question: q for q in goldset.questions}

    def generate(self, question):
        q = self.by_question[question]
        return Prediction(answerable=q.answerable, sql=q.sql or "", model=self.model)


class FixedGenerator:
    model = "fixe"

    def __init__(self, sql, answerable=True):
        self.sql, self.answerable = sql, answerable

    def generate(self, question):
        return Prediction(answerable=self.answerable, sql=self.sql, model=self.model)


def test_goldset_references_all_run(goldset, engine):
    issues, row_counts = validate_goldset(goldset, engine)
    assert not [i for i in issues if i.level == ERROR], issues
    assert all(n > 0 for n in row_counts.values()), row_counts


def test_schema_description_lists_tables_and_foreign_keys(engine):
    text = describe_schema(engine)
    for table in ("clients", "commandes", "lignes_commande", "paiements", "produits", "regions"):
        assert f"TABLE {table}" in text
    assert "-> clients.client_id" in text
    assert "commande_id INTEGER PRIMARY KEY" in text
    assert "valeurs fréquentes" not in text


def test_schema_samples_are_opt_in(engine):
    text = describe_schema(engine, include_samples=True)
    assert "statut VARCHAR NOT NULL  -- valeurs fréquentes : 'payee'" in text


def test_oracle_gets_full_marks(goldset, engine):
    results = run_baseline(goldset, engine, OracleGenerator(goldset), concurrency=2)
    summary = summarize(results, "oracle")
    assert summary["execution_accuracy"] == 1.0
    assert summary["abstention_accuracy"] == 1.0
    assert {r.status for r in results} == {CORRECT, RIGHT_ABSTENTION}


def test_failures_are_classified(goldset, engine):
    wrong = run_baseline(goldset, engine, FixedGenerator("SELECT 42 AS x"), limit=3)
    assert {r.status for r in wrong} == {INCORRECT}

    broken = run_baseline(goldset, engine, FixedGenerator("SELECT colonne_inexistante FROM clients"), limit=1)
    assert broken[0].status == SQL_ERROR

    unsafe = run_baseline(goldset, engine, FixedGenerator("DELETE FROM clients"), limit=1)
    assert unsafe[0].status == SQL_REJECTED

    eager = run_baseline(goldset, engine, FixedGenerator("SELECT 1"))
    assert {r.status for r in eager if not r.expected_answerable} == {WRONG_ANSWER}


def test_report_renders(goldset, engine):
    results = run_baseline(goldset, engine, FixedGenerator("SELECT 42 AS x"), limit=4)
    meta = {"started_at": "2026-11-11 10:00", "model": "fixe", "effort": "high", "samples": False, "fallbacks": True}
    markdown = render_markdown(goldset, results, summarize(results, "fixe"), meta)
    assert "Gate 0" in markdown and "q001" in markdown


def test_qir_coverage_of_demo(goldset):
    cov = qir_coverage(goldset)
    assert cov["annotated"] == cov["answerable"] == 12
    assert cov["not_expressible"] == ["q010"]
