from datetime import date

from qraft import Compiler, Dimension, Filter, Intent, Measure, Ranking, Ratio, TimeScope
from qraft.models import INSTRUCTIONS, StaticModel
from qraft_forge import ExecutionChecker, IntentSampler, RoundTrip, TemplateVerbalizer, dataset_stats, split_by_intent
from qraft_forge.checks import model_interpreter, rule_interpreter
from qraft_forge.dataset import SPLITS, read_jsonl, record_intent
from qraft_forge.verbalize import Noun, pluralize

CA_2025_BY_REGION = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="region"),),
                           time=TimeScope(start="2025-01-01", end="2026-01-01"))


def _records(directory):
    return [r for split in SPLITS for r in read_jsonl(directory / f"{split}.jsonl")]


def test_sampler_is_deterministic(graph):
    first = IntentSampler(graph, seed=7).sample(25)
    again = IntentSampler(graph, seed=7).sample(25)
    other = IntentSampler(graph, seed=8).sample(25)
    assert [s.fingerprint for s in first] == [s.fingerprint for s in again]
    assert len({s.fingerprint for s in first}) == 25  # dédoublonnage par empreinte
    assert [s.fingerprint for s in other] != [s.fingerprint for s in first]


def test_sampler_covers_every_metric_and_dimension(run, graph):
    coverage = run.stats["coverage"]
    for metric in graph.semantic.metrics:
        assert coverage[f"metric:{metric}"] >= 2, metric
    for dim in graph.semantic.dimensions:
        assert coverage[f"dim:{dim}"] >= 2, dim
    for grain in ("month", "quarter", "year"):
        assert coverage[f"time:{grain}"] >= 2, grain
    assert run.stats["under_covered"] == []
    assert {"mesure_dim", "serie", "top_k", "comparaison", "ratio"} <= set(run.stats["motifs"])


def test_sampled_values_and_periods_come_from_profiles(run, graph):
    for sample in run.samples:
        intent = sample.intent
        filters = list(intent.filters) + [f for r in intent.ratios for f in r.numerator.filters]
        for f in filters:
            _, column = graph.column(f.column)
            assert f.value in column.values
        if intent.time and intent.time.start:
            start, end = date.fromisoformat(intent.time.start), date.fromisoformat(intent.time.end)
            assert date(2023, 1, 1) <= start < end <= date(2026, 3, 1)


def test_checker_rejects_errors_empty_and_degenerate_results(graph, source):
    checker = ExecutionChecker(graph, source)
    unknown = Intent(measures=(Measure(metric="marge"),))
    cancelled = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="region"),),
                       filters=(Filter(column="commandes.statut", value="annulee"),))  # le CA ne compte que les payées
    future = Intent(measures=(Measure(metric="ca"),), time=TimeScope(start="2030-01-01", end="2031-01-01"))
    paid = Filter(column="commandes.statut", value="payee")  # déjà un filtre intrinsèque du CA : part de 100 %
    always_full = Intent(ratios=(Ratio(numerator=Measure(metric="ca", filters=(paid,)),
                                       denominator=Measure(metric="ca")),))
    for intent in (unknown, cancelled, future, always_full):
        assert checker.check(intent) is None
    assert dict(checker.rejections) == {"compilation": 1, "vide": 1, "degenere": 2}
    reference = checker.check(CA_2025_BY_REGION)
    assert reference is not None and reference.row_count == 6


def test_exported_pairs_all_execute(dataset_dir, graph, source):
    records = _records(dataset_dir)
    assert len(records) >= 60
    compiler = Compiler(graph)
    summary = graph.summary_for_prompt()
    for record in records:
        system, user, assistant = record["messages"]
        assert system["content"].startswith(INSTRUCTIONS) and summary in system["content"]
        assert user["role"] == "user" and user["content"]
        intent = record_intent(record)
        assert intent.fingerprint() == record["metadata"]["fingerprint"]
        result = source.query(compiler.compile(intent).sql)
        assert result.rows and len(result.rows) == record["metadata"]["rows"]


def test_roundtrip_keeps_a_majority(run):
    questions = run.stats["questions"]
    assert questions["generated"] >= 120
    assert questions["kept"] / questions["generated"] > 0.5
    assert run.stats["intents"]["with_pairs"] >= 50


def test_roundtrip_filters_wrong_questions(graph, source):
    checker = ExecutionChecker(graph, source)
    reference = checker.run(CA_2025_BY_REGION)
    rules = RoundTrip(checker, rule_interpreter(graph))
    assert rules.verify(reference, "Chiffre d'affaires par région en 2025").kept
    assert rules.verify(reference, "Quelles sont les ventes 2025 selon la région ?").kept
    assert rules.verify(reference, "Chiffre d'affaires par région en 2024").reason == "resultat_different"
    assert rules.verify(reference, "Quelle météo demain ?").reason == "non_interpretee"
    # Un modèle injecté peut remplacer le parseur ; même résultat avec une autre écriture (colonne brute).
    same_rows = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="regions.nom"),),
                       time=TimeScope(start="2025-01-01", end="2026-01-01"))
    model = RoundTrip(checker, model_interpreter(StaticModel({"CA 2025 par région": [same_rows]})))
    outcome = model.verify(reference, "CA 2025 par région")
    assert outcome.kept and outcome.reason == "meme_resultat"


def test_no_leak_between_train_and_validation(dataset_dir):
    train = read_jsonl(dataset_dir / "train.jsonl")
    validation = read_jsonl(dataset_dir / "validation.jsonl")
    assert train and validation
    assert not {r["metadata"]["fingerprint"] for r in train} & {r["metadata"]["fingerprint"] for r in validation}
    assert not {r["messages"][1]["content"] for r in train} & {r["messages"][1]["content"] for r in validation}
    stats = dataset_stats(dataset_dir)
    assert stats["leaked_intents"] == 0 and stats["leaked_questions"] == 0
    assert stats["manifest"]["funnel"]["questions"]["kept"] >= len(train) + len(validation)


def test_split_is_deterministic_and_grouped_by_intent(run):
    a = split_by_intent(run.pairs, val_ratio=0.2, seed=1)
    b = split_by_intent(run.pairs, val_ratio=0.2, seed=1)
    assert a == b
    assert not {p.fingerprint for p in a["train"]} & {p.fingerprint for p in a["validation"]}
    assert len(a["train"]) + len(a["validation"]) == len(run.pairs)


def test_verbalizer_varies_wording(graph):
    verbalizer = TemplateVerbalizer(graph, seed=3)
    questions = verbalizer.questions(CA_2025_BY_REGION, k=4)
    assert len(questions) == 4 and len(set(questions)) == 4
    assert all("2025" in q and "_" not in q for q in questions)
    top = Intent(measures=(Measure(metric="quantite_vendue"),), dimensions=(Dimension(column="produit"),),
                 ranking=Ranking(direction="desc", limit=5))
    ranked = verbalizer.questions(top, k=3)
    assert ranked and all("5" in q for q in ranked)


def test_french_agreement_helpers():
    assert Noun.guess("quantité vendue").det() == "la quantité vendue"
    assert Noun.guess("chiffre d'affaires").de() == "du chiffre d'affaires"
    assert Noun.guess("ventes").de() == "des ventes"
    assert Noun.guess("encaissé").det() == "l'encaissé"
    assert pluralize("canal") == "canaux" and pluralize("type de client") == "types de client"
