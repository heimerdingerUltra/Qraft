import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from qraft import Engine
from qraft_server.app import create_app
from qraft_server.mcp_server import QraftTools, build_mcp

SEMANTIC = Path(__file__).resolve().parents[3] / "examples" / "ventes.semantic.yaml"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    from qraft_eval.demo import build_demo_db

    path = build_demo_db(tmp_path_factory.mktemp("demo") / "ventes.duckdb")
    return Engine.from_url(f"duckdb:///{path.as_posix()}", semantic=SEMANTIC)


@pytest.fixture(scope="module")
def client(engine):
    return TestClient(create_app(engine, trust_identity_headers=True))


def test_health(client):
    data = client.get("/v1/health").json()
    assert data["status"] == "ok" and data["tables"] == 6


def test_ask_follow_up_feedback_and_result(client):
    first = client.post("/v1/questions", json={"question": "Chiffre d'affaires 2025 par région"}).json()
    assert first["status"] == "ok" and len(first["rows"]) == 6
    second = client.post("/v1/questions", json={"question": "et par mois ?", "previous_id": first["id"]}).json()
    assert second["columns"] == ["region", "mois", "ca"]
    assert client.post(f"/v1/questions/{first['id']}/feedback", json={"rating": 1}).status_code == 200
    assert client.post("/v1/questions/inconnu/feedback", json={"rating": 1}).status_code == 404
    rows = client.get(f"/v1/results/{first['id']}").json()["rows"]
    assert len(rows) == 6


def test_arrow_export(client):
    pa = pytest.importorskip("pyarrow")
    answer = client.post("/v1/questions", json={"question": "Combien de clients avons-nous ?"}).json()
    response = client.get(f"/v1/results/{answer['id']}?format=arrow")
    table = pa.ipc.open_stream(response.content).read_all()
    assert table.num_rows == 1


def test_stream_emits_ordered_events(client):
    with client.stream("GET", "/v1/questions/stream", params={"q": "Chiffre d'affaires 2025 par catégorie"}) as resp:
        body = "".join(resp.iter_text())
    events = [line.split(":", 1)[1].strip() for line in body.splitlines() if line.startswith("event:")]
    assert events[:3] == ["interpretation", "sql", "rows"] and events[-1] == "done"


def test_stream_abstention(client):
    with client.stream("GET", "/v1/questions/stream", params={"q": "Quel est le taux de satisfaction ?"}) as resp:
        body = "".join(resp.iter_text())
    assert "event: abstention" in body


def test_bad_requests(client):
    assert client.post("/v1/questions", json={}).status_code == 400
    assert client.get("/v1/questions/stream").status_code == 400


def test_knowledge_endpoints(client):
    results = client.get("/v1/knowledge/search", params={"q": "chiffre"}).json()["results"]
    assert any(r["ref"] == "ca" for r in results)
    metrics = client.get("/v1/knowledge/metrics").json()["metrics"]
    assert {m["id"] for m in metrics} >= {"ca", "encaisse"}


def test_mcp_tools(engine):
    tools = QraftTools(engine)
    answer = tools.ask("Combien de clients avons-nous ?")
    assert answer["status"] == "ok"
    assert tools.get_result(answer["id"])["total"] == 1
    assert tools.describe_metric("ca")["unit"] == "EUR"
    assert "error" in tools.describe_metric("inconnue")
    server = build_mcp(engine)
    listed = asyncio.run(server.list_tools())
    assert {t.name for t in listed} == {"ask", "search_knowledge", "describe_metric", "get_result"}
