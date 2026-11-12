from pathlib import Path

import pytest

from qraft import Source
from qraft.engine import build_graph
from qraft_forge import ForgeConfig, forge, write_dataset

REPO = Path(__file__).resolve().parents[3]
SEMANTIC = REPO / "examples" / "ventes.semantic.yaml"


@pytest.fixture(scope="session")
def demo_url(tmp_path_factory) -> str:
    from qraft_eval.demo import build_demo_db

    path = build_demo_db(tmp_path_factory.mktemp("demo") / "ventes.duckdb")
    return f"duckdb:///{path.as_posix()}"


@pytest.fixture(scope="session")
def source(demo_url) -> Source:
    return Source(demo_url)


@pytest.fixture(scope="session")
def graph(source):
    return build_graph(source, semantic=SEMANTIC)


@pytest.fixture(scope="session")
def run(graph, source):
    return forge(graph, source, ForgeConfig(n=60, seed=42, per_intent=3, min_coverage=2))


@pytest.fixture(scope="session")
def dataset_dir(run, graph, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("jeu")
    write_dataset(run.pairs, out, graph, val_ratio=0.15, seed=42, extra={"funnel": run.stats})
    return out
