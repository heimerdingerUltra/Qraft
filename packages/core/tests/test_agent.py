import itertools
import json
from types import SimpleNamespace

from qraft import Engine, Policy
from qraft.agent import LAST_STEP_NOTICE, PREVIEW_ROWS, PROBE_LIMIT, AnalystAgent, Budget

from .conftest import SEMANTIC

REGION_2025 = "Chiffre d'affaires 2025 par région"


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool(id_, name, payload):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=payload)


def reply(content, stop_reason, tokens=(100, 50), **extra):
    usage = SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage, stop_details=None, **extra)


class ScriptedClient:
    """Faux client anthropic : rejoue une suite de réponses et garde chaque requête."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        try:
            return next(self.responses)
        except StopIteration:
            raise AssertionError("appel au modèle non prévu par le script") from None


def results_of(call):
    """Blocs tool_result du dernier message utilisateur d'une requête, décodés quand c'est du JSON."""
    last = call["messages"][-1]
    assert last["role"] == "user"
    out = {}
    for block in last["content"]:
        if block["type"] == "tool_result":
            content = block["content"]
            out[block["tool_use_id"]] = (json.loads(content) if not block.get("is_error") else content,
                                         block.get("is_error", False))
    return out


def _assert_strict(schema):
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == sorted(schema["properties"])
        for sub in schema["properties"].values():
            _assert_strict(sub)
    for key in ("items",):
        if isinstance(schema.get(key), dict):
            _assert_strict(schema[key])
    for option in schema.get("anyOf", []):
        _assert_strict(option)


def test_agent_runs_parallel_tools_and_groups_results(engine):
    first = reply([SimpleNamespace(type="thinking", thinking="", signature="sig"), text("Je commence."),
                   tool("t1", "ask", {"question": REGION_2025}),
                   tool("t2", "search_knowledge", {"query": "panier moyen"})], "tool_use")
    second = reply([tool("t3", "transform", {"sql": "SELECT region, ca FROM r1 ORDER BY ca DESC LIMIT 2"}),
                    tool("t4", "chart", {"ref": "r1", "dimensions": None})], "tool_use")
    final = reply([text("Deux régions mènent [r2], d'après le chiffre d'affaires par région [r1].")], "end_turn")
    client = ScriptedClient([first, second, final])
    report = AnalystAgent(engine, client=client).run("Quelles régions portent le chiffre d'affaires 2025 ?")

    assert report.status == "termine" and report.message is None
    assert report.report.startswith("Deux régions mènent [r2]")
    assert len(client.calls) == 3 and report.steps == 3 and report.tokens == 450

    # Requête conforme : modèle, effort, outils stricts, tool_choice auto, aucun paramètre thinking.
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["max_tokens"] == 16000
    assert call["output_config"] == {"effort": "high"} and call["tool_choice"] == {"type": "auto"}
    assert "thinking" not in call and "Domaine" in call["system"]
    assert {t["name"] for t in call["tools"]} == {"search_knowledge", "ask", "probe", "transform",
                                                  "explain_change", "chart"}
    for definition in call["tools"]:
        assert definition["strict"] is True
        _assert_strict(definition["input_schema"])

    # Le contenu de l'assistant repart intégralement, puis UN message utilisateur avec TOUS les résultats.
    history = client.calls[1]["messages"]
    assert history[-2] == {"role": "assistant", "content": first.content}
    assert history[-2]["content"] is first.content
    assert [b["tool_use_id"] for b in history[-1]["content"]] == ["t1", "t2"]
    results = results_of(client.calls[1])
    (ask, ask_error), (search, search_error) = results["t1"], results["t2"]
    assert not ask_error and not search_error
    assert ask["ref"] == "r1" and ask["colonnes"] == ["region", "ca"] and ask["nb_lignes"] == 6
    assert ask["statut"] == "ok" and ask["confiance"]["niveau"]
    assert any(r["ref"] == "panier_moyen" for r in search["resultats"])

    results = results_of(client.calls[2])
    transformed, _ = results["t3"]
    assert transformed["ref"] == "r2" and len(transformed["lignes"]) == 2
    top_two = sorted(ask["lignes"], key=lambda r: r[1], reverse=True)[:2]
    assert [r[0] for r in transformed["lignes"]] == [r[0] for r in top_two]
    chart, _ = results["t4"]
    assert chart["graphique"]["mark"] == "bar" and report.charts[0]["ref"] == "r1"

    assert [s.ref for s in report.citations] == ["r1", "r2"]
    assert report.sub_answers[0].request == REGION_2025 and report.sub_answers[0].interpretation
    assert [(t.kind, t.name) for t in report.trace] == [
        ("modele", "claude-opus-5-5"), ("outil", "ask"), ("outil", "search_knowledge"),
        ("modele", "claude-opus-5-5"), ("outil", "transform"), ("outil", "chart"), ("modele", "claude-opus-5-5")]
    assert report.db_calls == 1


def test_tool_failures_are_returned_as_errors(engine):
    calls = reply([
        tool("p1", "probe", {"sql": "DELETE FROM clients"}),
        tool("p2", "probe", {"sql": "SELECT nom, segment FROM clients ORDER BY nom LIMIT 500"}),
        tool("x1", "transform", {"sql": "SELECT * FROM 'secret.csv'"}),
        tool("x2", "transform", {"sql": "SELECT * FROM r9"}),
        tool("e1", "explain_change", {"metric": "ca", "period_a": {"start": "2024-13-01", "end": "2025-01-01"},
                                      "period_b": {"start": "2025-01-01", "end": "2026-01-01"},
                                      "dimensions": [], "filters": []}),
        tool("u1", "inconnu", {}),
    ], "tool_use")
    client = ScriptedClient([calls, reply([text("Fin.")], "end_turn")])
    report = AnalystAgent(engine, client=client).run("Explore les clients.")
    results = results_of(client.calls[1])
    assert {k for k, (_, error) in results.items() if error} == {"p1", "x1", "x2", "e1", "u1"}
    probe, _ = results["p2"]
    assert probe["nb_lignes"] == PROBE_LIMIT and probe["sql_execute"].endswith(f"LIMIT {PROBE_LIMIT}")
    assert len(probe["lignes"]) == PREVIEW_ROWS
    assert "table inconnue" in results["x2"][0] and "AAAA-MM-JJ" in results["e1"][0]
    assert report.db_calls == 1 and report.status == "termine"
    assert [t.status for t in report.trace if t.kind == "outil"].count("erreur") == 5


def test_agent_respects_step_budget(engine):
    counter = itertools.count(1)
    endless = (reply([tool(f"t{next(counter)}", "search_knowledge", {"query": "ventes"})], "tool_use")
               for _ in itertools.count())
    client = ScriptedClient(endless)
    report = AnalystAgent(engine, client=client, budget=Budget(max_steps=2)).run("Analyse sans fin")
    assert report.status == "budget_epuise" and "budget" in report.message
    assert len(client.calls) == 2 and report.steps == 2
    # Le dernier appel autorisé est prévenu, et les outils demandés ensuite ne sont pas exécutés.
    assert client.calls[1]["messages"][-1]["content"][-1] == {"type": "text", "text": LAST_STEP_NOTICE}
    assert sum(1 for t in report.trace if t.kind == "outil") == 1


def test_agent_respects_db_and_token_budgets(engine):
    parallel = reply([tool("a1", "ask", {"question": REGION_2025}),
                      tool("a2", "ask", {"question": "Chiffre d'affaires 2024 par région"})], "tool_use")
    client = ScriptedClient([parallel, reply([text("Conclusion partielle [r1].")], "end_turn")])
    report = AnalystAgent(engine, client=client, budget=Budget(max_db_calls=1)).run("Compare 2024 et 2025")
    results = results_of(client.calls[1])
    assert not results["a1"][1] and results["a2"][1] and "budget" in results["a2"][0]
    assert report.db_calls == 1 and report.status == "termine"

    greedy = ScriptedClient([reply([tool("a1", "ask", {"question": REGION_2025})], "tool_use", tokens=(1500, 600))])
    report = AnalystAgent(engine, client=greedy, budget=Budget(max_tokens=2000)).run("Analyse coûteuse")
    assert report.status == "budget_epuise" and len(greedy.calls) == 1
    assert report.tokens == 2100 and report.db_calls == 0


def test_agent_stops_cleanly_on_refusal_and_truncation(engine):
    refusal = reply([], "refusal")
    refusal.stop_details = SimpleNamespace(category="cyber", explanation=None)
    client = ScriptedClient([refusal])
    report = AnalystAgent(engine, client=client, fallbacks=True).run("Demande refusée")
    assert report.status == "refus" and "cyber" in report.message
    assert client.calls[0]["fallbacks"] == "default"

    truncated = ScriptedClient([reply([text("Rapport inachevé")], "max_tokens")])
    report = AnalystAgent(engine, client=truncated).run("Rapport trop long")
    assert report.status == "tronque" and report.report == "Rapport inachevé"


def test_agent_explain_change_tool(engine):
    call = tool("c1", "explain_change", {
        "metric": "ca", "period_a": {"start": "2024-01-01", "end": "2025-01-01"},
        "period_b": {"start": "2025-01-01", "end": "2026-01-01"},
        "dimensions": ["region", "canal"], "filters": [{"dimension": "segment", "value": "PME"}]})
    client = ScriptedClient([reply([call], "tool_use"), reply([text("Analyse [r1].")], "end_turn")])
    report = AnalystAgent(engine, client=client).run("Pourquoi le CA des PME a-t-il changé ?")
    payload, error = results_of(client.calls[1])["c1"]
    assert not error and payload["ref"] == "r1"
    assert payload["filtres"] == ["segment = PME"] and payload["narration"].startswith("Chiffre d'affaires (")
    assert report.db_calls == payload["requetes"]
    segments = report.sub_answers[0].rows
    by_dimension = {}
    for dimension, _, _, _, contribution, _ in segments:
        by_dimension[dimension] = by_dimension.get(dimension, 0) + contribution
    assert all(abs(float(total) - payload["ecart"]) < 1e-6 for total in by_dimension.values())


def test_restricted_data_never_reaches_the_model(demo_url):
    engine = Engine.from_url(demo_url, semantic=SEMANTIC, policy=Policy(restricted_tables={"clients"}))
    calls = reply([tool("a1", "ask", {"question": "Chiffre d'affaires 2025 par segment"}),
                   tool("p1", "probe", {"sql": "SELECT nom FROM clients"})], "tool_use")
    client = ScriptedClient([calls, reply([text("Impossible.")], "end_turn")])
    agent = AnalystAgent(engine, client=client)
    assert "- clients(" not in agent.system
    agent.run("CA par segment")
    results = results_of(client.calls[1])
    assert all(error and "restreintes" in content for content, error in results.values())
