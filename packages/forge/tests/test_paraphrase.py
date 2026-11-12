import json
import re
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from qraft import Dimension, Intent, Measure, TimeScope
from qraft_forge import ClaudeParaphraser, ExecutionChecker, Pair, roundtrip_pairs
from qraft_forge.checks import rule_interpreter
from qraft_forge.cli import app
from qraft_forge.verbalize import parse_paraphrases

SEMANTIC = Path(__file__).resolve().parents[3] / "examples" / "ventes.semantic.yaml"

CA_2025_BY_REGION = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="region"),),
                           time=TimeScope(start="2025-01-01", end="2026-01-01"))
ORDERS_2024 = Intent(measures=(Measure(agg="count", column="commandes"),),
                     time=TimeScope(start="2024-01-01", end="2025-01-01"))
BASKET_BY_CHANNEL = Intent(measures=(Measure(metric="panier_moyen"),), dimensions=(Dimension(column="canal"),))


class FakeBatches:
    """Faux client Message Batches : enregistre les requêtes, rend des résultats dans le désordre."""

    def __init__(self) -> None:
        self.created: list[list[dict]] = []
        self.items: list[SimpleNamespace] = []

    def create(self, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id="msgbatch_test", processing_status="in_progress")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status="ended")

    def results(self, batch_id):
        yield from self.items


def _succeeded(custom_id: str, text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    message = SimpleNamespace(content=blocks, stop_reason=stop_reason)
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message))


def _failed(custom_id: str, kind: str) -> SimpleNamespace:
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=kind, error=SimpleNamespace(type="api_error")))


def _pairs() -> list[Pair]:
    return [
        Pair("Chiffre d'affaires par région en 2025", CA_2025_BY_REGION, "mesure_dim", 6),
        Pair("Combien de commandes en 2024 ?", ORDERS_2024, "mesure", 1),
        Pair("Panier moyen par canal", BASKET_BY_CHANNEL, "mesure_dim", 3),
    ]


def test_paraphraser_builds_batch_requests_and_reads_results_by_custom_id(graph, source, tmp_path):
    batches = FakeBatches()
    paraphraser = ClaudeParaphraser(graph, client=SimpleNamespace(messages=SimpleNamespace(batches=batches)))
    pairs = _pairs()
    batch_id = paraphraser.submit(pairs)
    assert batch_id == "msgbatch_test" and paraphraser.is_done(batch_id)

    requests = batches.created[0]
    ids = [r["custom_id"] for r in requests]
    assert len(requests) == 3 and len(set(ids)) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", i) for i in ids)
    for request, pair in zip(requests, pairs):
        params = request["params"]
        assert params["model"] == "claude-opus-5-5"
        assert params["output_config"] == {"effort": "low"}
        assert isinstance(params["max_tokens"], int) and params["max_tokens"] > 0
        assert params["messages"][0]["role"] == "user" and pair.question in params["messages"][0]["content"]
        assert not {"temperature", "fallbacks", "stream", "thinking"} & set(params)

    # Résultats dans le désordre, avec un échec, un identifiant inconnu et une réponse en liste à puces.
    batches.items = [
        _succeeded(ids[2], '["Panier moyen selon le canal de vente", "Valeur moyenne des commandes par canal"]'),
        _failed(ids[1], "errored"),
        _succeeded("q999999-inconnu", '["question orpheline"]'),
        _succeeded(ids[0], "- CA par région en 2025\n- Ventes par région en 2024\n- Chiffre d'affaires par région en 2025"),
    ]
    found = paraphraser.collect(batch_id)
    assert set(found) == {ids[0], ids[2]}
    assert found[ids[0]] == ["CA par région en 2025", "Ventes par région en 2024"]  # l'original est écarté
    assert paraphraser.failures == {"errored": 1, "inconnu": 1}

    new_pairs = paraphraser.pairs_from(found)
    assert {p.origin for p in new_pairs} == {"claude"}
    by_question = {p.question: p for p in new_pairs}
    assert by_question["Valeur moyenne des commandes par canal"].intent == BASKET_BY_CHANNEL
    assert by_question["CA par région en 2025"].intent == CA_2025_BY_REGION

    # Les paraphrases repassent l'aller-retour : la question qui change d'année est écartée.
    kept, roundtrip = roundtrip_pairs(new_pairs, ExecutionChecker(graph, source), rule_interpreter(graph))
    assert {p.question for p in kept} == {"CA par région en 2025", "Panier moyen selon le canal de vente",
                                          "Valeur moyenne des commandes par canal"}
    assert roundtrip.outcomes["resultat_different"] == 1

    # L'index custom_id -> paire survit à un redémarrage (un lot peut durer 24 h).
    paraphraser.save_index(tmp_path / "index.jsonl")
    restored = ClaudeParaphraser(graph, client=object())
    restored.load_index(tmp_path / "index.jsonl")
    assert restored.index == paraphraser.index


def test_paraphraser_prepares_requests_without_client(graph):
    paraphraser = ClaudeParaphraser(graph)  # aucun client anthropic créé tant qu'on n'envoie rien
    requests = paraphraser.build_requests(_pairs()[:1])
    assert requests[0]["params"]["system"][0]["text"].startswith("Tu reformules")
    assert paraphraser._client is None


def test_parse_paraphrases_accepts_json_or_lines():
    assert parse_paraphrases('Voici :\n["A ?", "B ?", "a ?"]') == ["A ?", "B ?"]
    assert parse_paraphrases("1. Première\n2) Deuxième\n\n- Troisième", limit=2) == ["Première", "Deuxième"]


def test_cli_generate_then_stats(demo_url, tmp_path):
    out = tmp_path / "jeu"
    runner = CliRunner()
    result = runner.invoke(app, ["generate", "--db", demo_url, "--semantic", str(SEMANTIC), "--out", str(out),
                                 "--n", "20", "--seed", "7", "--paraphrase-requests"])
    assert result.exit_code == 0, result.output
    for name in ("train.jsonl", "validation.jsonl", "manifest.json", "paraphrase_requests.jsonl",
                 "paraphrase_index.jsonl"):
        assert (out / name).exists(), name
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["funnel"]["intents"]["verified"] == 20
    first = json.loads((out / "paraphrase_requests.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["params"]["model"] == "claude-opus-5-5"

    stats = runner.invoke(app, ["stats", str(out)])
    assert stats.exit_code == 0, stats.output
    assert "Fuite train/validation : 0 intention(s), 0 question(s)" in stats.output
