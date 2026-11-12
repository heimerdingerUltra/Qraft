import json
from types import SimpleNamespace

from qraft import Compiler, Intent, Measure, TimeScope
from qraft.confidence import Calibrator, estimate
from qraft.models import ClaudeModel, LocalModel, qir_json_schema
from qraft.verify import DistinguishingDB, same_results


def test_distinguishing_db_separates_different_meanings(graph):
    with_rule = Compiler(graph).compile(Intent(measures=(Measure(metric="ca"),))).sql
    without_rule = Compiler(graph).compile(
        Intent(measures=(Measure(agg="sum", column="lignes_commande.prix_unitaire"),))).sql
    counter = DistinguishingDB(graph).find(with_rule, without_rule, ["lignes_commande", "commandes"], dialect="duckdb")
    assert counter is not None and counter.result_a != counter.result_b


def test_distinguishing_db_accepts_equivalent_queries(graph):
    a = "SELECT COUNT(*) FROM clients"
    b = "SELECT COUNT(client_id) FROM clients"
    assert DistinguishingDB(graph).find(a, b, ["clients"], dialect="duckdb") is None


def test_same_results_normalizes_types():
    assert same_results([(1, "a")], [(1.0, "a")])
    assert not same_results([(1,)], [(2,)])


def test_confidence_penalizes_risks():
    clean = estimate(route="regles", coverage=1.0, compiled_signals={}, check_flags={}, assumptions=0)
    risky = estimate(route="modele", coverage=0.8, compiled_signals={"fanout": True, "ambiguous_join": True},
                     check_flags={"empty": True}, assumptions=2)
    assert clean.tier == "vérifié" and risky.tier == "incertain" and risky.reasons


def test_calibrator_is_monotonic():
    cal = Calibrator().fit([0.1, 0.4, 0.5, 0.9, 0.95], [False, True, False, True, True])
    values = [cal.transform(s) for s in (0.0, 0.3, 0.6, 1.0)]
    assert values == sorted(values)
    assert Calibrator.expected_calibration_error([0.9, 0.9], [True, True]) < 0.2


def test_qir_schema_lists_real_identifiers(graph):
    schema = qir_json_schema(graph)
    metric_enum = schema["properties"]["measures"]["items"]["properties"]["metric"]["anyOf"][0]["enum"]
    assert "ca" in metric_enum and schema["additionalProperties"] is False


class _FakeAnthropic:
    def __init__(self, payload):
        block = SimpleNamespace(type="text", text=json.dumps(payload))
        response = SimpleNamespace(content=[block], stop_reason="end_turn", model="claude-opus-5-5")
        self.calls = []

        def create(**kwargs):
            self.calls.append(kwargs)
            return response

        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_claude_model_decodes_structured_output(graph):
    payload = {"measures": [{"metric": "ca", "agg": None, "column": None, "filters": [], "alias": None}],
               "ratios": [], "dimensions": [], "filters": [], "time": None, "ranking": None, "assumptions": [],
               "escape_sql": None, "needs_context": [], "answerable": True}
    client = _FakeAnthropic(payload)
    intents = ClaudeModel(graph, client=client).propose("CA ?")
    assert intents == [Intent(measures=(Measure(metric="ca"),))]
    call = client.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][0]["cache_control"]["type"] == "ephemeral"
    assert call["fallbacks"] == "default"


def test_local_model_uses_constrained_decoding(graph):
    sent = {}

    class FakeHttp:
        def post(self, path, json):
            sent.update(json)
            content = '{"measures": [{"agg": "count", "column": "clients", "filters": []}]}'
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"choices": [{"message": {"content": content}}]})

    intents = LocalModel(graph, client=FakeHttp()).propose("Combien de clients ?")
    assert intents[0].measures[0].column == "clients"
    assert sent["response_format"]["type"] == "json_schema"
