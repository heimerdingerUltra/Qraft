import json

import yaml

from qraft import Engine, Filter, Intent, JsonlEventSink, Measure, Policy, TimeScope
from qraft.models import StaticModel
from qraft.parser import RuleParser
from qraft.verify import same_results

from .conftest import GOLDSET, SEMANTIC


def test_demo_goldset_without_any_model(engine):
    gold = yaml.safe_load(GOLDSET.read_text(encoding="utf-8"))
    correct = abstained_right = 0
    for q in gold["questions"]:
        answer = engine.ask(q["question"])
        if q.get("answerable", True):
            if answer.rows and same_results(engine.source.query(q["sql"]).rows, answer.rows):
                correct += 1
        elif answer.status == "abstention":
            abstained_right += 1
    assert correct >= 11 and abstained_right == 2


def test_parser_understands_periods_rankings_and_values(graph):
    parser = RuleParser(graph)
    result = parser.parse("Top 3 des produits par quantité vendue au deuxième trimestre 2025 sur le canal web")
    intent = result.intent
    assert result.handled
    assert intent.ranking.limit == 3
    assert intent.time.start == "2025-04-01" and intent.time.end == "2025-07-01"
    assert Filter(column="commandes.canal", value="web") in intent.filters
    assert parser.parse("Quels clients n'ont jamais commandé ?").handled is False


def test_follow_up_extends_previous_intent(engine):
    first = engine.ask("Chiffre d'affaires 2025 par région")
    second = first.follow_up("et par mois ?")
    assert second.columns == ["region", "mois", "ca"]
    third = first.follow_up("et en 2024 ?")
    assert third.intent.time.start == "2024-01-01"


def test_abstention_explains_unknown_words(engine):
    answer = engine.ask("Quelle est la marge brute par produit ?")
    assert answer.status == "abstention" and "marge" in answer.message


def test_model_route_and_clarification(demo_url):
    q = "Quel est le chiffre d'affaires total ?"
    with_status = Intent(measures=(Measure(metric="ca"),))
    without_status = Intent(measures=(Measure(agg="sum", column="paiements.montant"),))
    model = StaticModel({q: [with_status, without_status]})
    engine = Engine.from_url(demo_url, semantic=SEMANTIC, model=model, samples=2)
    engine.parser.parse = lambda *a, **k: type("R", (), {"handled": False, "coverage": 0.0, "unmatched": [],
                                                         "reason": None, "intent": Intent()})()
    answer = engine.ask(q)
    assert answer.status == "clarification"
    assert len(answer.alternatives) == 2 and "Exemple" in answer.message


def test_events_are_recorded(demo_url, tmp_path):
    sink = JsonlEventSink(tmp_path / "events.jsonl")
    engine = Engine.from_url(demo_url, semantic=SEMANTIC, events=sink)
    answer = engine.ask("Combien de clients avons-nous ?")
    engine.feedback(answer.id, rating=1)
    lines = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["type"] for l in lines] == ["answer", "feedback"]


def test_pack_round_trip(demo_url, tmp_path):
    pack = tmp_path / "pack.json"
    first = Engine.from_url(demo_url, semantic=SEMANTIC, pack=pack)
    second = Engine.from_url(demo_url, semantic=SEMANTIC, pack=pack)
    assert pack.exists() and first.graph.version == second.graph.version


def test_answer_serializes_and_suggests_chart(engine):
    answer = engine.ask("Chiffre d'affaires 2025 par mois")
    data = json.loads(json.dumps(answer.to_dict()))
    assert data["chart"]["mark"]["type"] == "line"
    assert data["confidence"]["tier"] in ("vérifié", "probable", "incertain")


def test_restricted_tables_never_go_to_a_cloud_model(demo_url):
    q = "question pour le cloud"
    model = StaticModel({q: [Intent(measures=(Measure(agg="count", column="clients"),))]}, cloud=True)
    engine = Engine.from_url(demo_url, semantic=SEMANTIC, model=model, policy=Policy(restricted_tables={"clients"}))
    assert engine.ask(q).status == "abstention"
