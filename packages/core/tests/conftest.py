from datetime import date
from pathlib import Path

import pytest

from qraft import Engine

REPO = Path(__file__).resolve().parents[3]
SEMANTIC = REPO / "examples" / "ventes.semantic.yaml"
GOLDSET = REPO / "phase0" / "goldsets" / "demo_ventes.yaml"


@pytest.fixture(scope="session")
def demo_url(tmp_path_factory) -> str:
    from qraft_eval.demo import build_demo_db

    path = build_demo_db(tmp_path_factory.mktemp("demo") / "ventes.duckdb")
    return f"duckdb:///{path.as_posix()}"


@pytest.fixture(scope="session")
def engine(demo_url) -> Engine:
    return Engine.from_url(demo_url, semantic=SEMANTIC, today=date(2026, 11, 12))


@pytest.fixture(scope="session")
def graph(engine):
    return engine.graph
