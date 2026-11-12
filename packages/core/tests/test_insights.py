import io
import math
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from qraft import Dimension, Filter, Intent, Measure, TimeScope
from qraft.insights import (
    AnomalyCondition,
    ConsoleNotifier,
    Monitor,
    QueryBudgetExceeded,
    ThresholdCondition,
    WebhookNotifier,
    detect_anomalies,
    explain_change,
    format_number,
    run_monitors,
)

Y2024 = TimeScope(start="2024-01-01", end="2025-01-01")
Y2025 = TimeScope(start="2025-01-01", end="2026-01-01")
MONTHLY_CA = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="time", grain="month"),))


def _value(engine, intent):
    return engine.source.query(engine.compile(intent).sql).rows[0][-1]


def _check_tree(node):
    """Invariant d'additivité : contributions + résidu = écart total, à chaque niveau."""
    assert node.delta == node.after - node.before
    for breakdown in node.breakdowns:
        assert breakdown.total_contribution + breakdown.residual == node.delta
        if node.kind == "additif":
            assert breakdown.residual == 0
            assert breakdown.total_contribution == node.delta
    if node.drilldown is not None:
        _check_tree(node.drilldown)


def test_contributions_sum_to_total_change(engine):
    ex = explain_change(engine, "ca", Y2024, Y2025)
    assert ex.kind == "additif"
    assert ex.before == _value(engine, Intent(measures=(Measure(metric="ca"),), time=Y2024))
    assert ex.after == _value(engine, Intent(measures=(Measure(metric="ca"),), time=Y2025))
    assert {b.dimension for b in ex.breakdowns} == {"region", "segment", "categorie", "canal", "produit", "client"}
    _check_tree(ex)
    # Récursion : le détail explique exactement la contribution du segment retenu, sans sa dimension.
    child = ex.drilldown
    assert child is not None and child.drilldown is None
    assert ex.focus.filter() in child.filters
    assert child.delta == ex.focus.contribution
    assert ex.focus.dimension not in {b.dimension for b in child.breakdowns}


def test_contributors_are_ranked_in_the_direction_of_the_change(engine):
    ex = explain_change(engine, "ca", Y2024, Y2025, top=2)
    direction = 1 if ex.delta > 0 else -1
    scores = [b.score for b in ex.breakdowns]
    assert scores == sorted(scores, reverse=True)
    assert ex.focus is ex.breakdowns[0].segments[0]
    for breakdown in ex.breakdowns:
        drivers = breakdown.drivers
        assert 1 <= len(drivers) <= 2
        assert all(s.contribution * direction > 0 for s in drivers)
        assert [abs(s.contribution) for s in drivers] == sorted((abs(s.contribution) for s in drivers), reverse=True)
        assert all(s.contribution * direction < 0 for s in breakdown.offsets)


def test_ratio_metric_splits_mix_and_rate_effects(engine):
    ex = explain_change(engine, "panier_moyen", Y2024, Y2025)
    assert ex.kind == "ratio"
    assert float(ex.before) == pytest.approx(_value(engine, Intent(measures=(Measure(metric="panier_moyen"),),
                                                                   time=Y2024)))
    assert ex.breakdowns
    for breakdown in ex.breakdowns:
        assert abs(breakdown.residual) < Decimal("1e-12")
        for s in breakdown.segments:
            assert s.contribution == s.mix_effect + s.rate_effect
    # Une commande peut contenir plusieurs catégories : la décomposition n'y serait pas additive.
    assert any("catégorie de produit" in w for w in ex.warnings)
    assert "categorie" not in {b.dimension for b in ex.breakdowns}
    assert "Effet mix" in ex.narrative
    _check_tree(ex)


def test_narrative_is_deterministic_and_quotes_computed_numbers(engine):
    ex = explain_change(engine, "ca", Y2024, Y2025, dimensions=["region", "canal", "segment"])
    again = explain_change(engine, "ca", Y2024, Y2025, dimensions=["region", "canal", "segment"])
    assert ex.narrative == again.narrative
    first = ex.narrative.splitlines()[0]
    assert first.startswith(f"Chiffre d'affaires : {format_number(ex.before, unit='EUR')} en 2024, "
                            f"{format_number(ex.after, unit='EUR')} en 2025, "
                            f"soit {format_number(ex.delta, unit='EUR', signed=True)}")
    best = ex.breakdowns[0]
    assert f"Par {best.label}" in ex.narrative
    assert f"{best.segments[0].label} ({format_number(best.segments[0].contribution, unit='EUR', signed=True)}" \
        in ex.narrative
    assert f"Dans « {ex.focus.label} »" in ex.narrative
    assert ex.to_dict()["narration"] == ex.narrative


def test_explain_change_with_filters_and_query_budget(engine):
    web = (Filter(column="canal", value="web"),)
    ex = explain_change(engine, "ca", Y2024, Y2025, dimensions=["region"], depth=1, filters=web)
    assert ex.query_count == 4  # deux totaux, deux décompositions
    assert ex.filters == web and ex.drilldown is None
    assert "canal de vente = web" in ex.narrative
    with pytest.raises(QueryBudgetExceeded):
        explain_change(engine, "ca", Y2024, Y2025, max_queries=3)


def test_format_number_is_french():
    assert format_number(Decimal("1234567.891"), unit="EUR") == "1 234 567,89 €"
    assert format_number(Decimal("-1500"), unit="EUR", signed=True) == "-1 500,00 €"
    assert format_number(12, signed=True) == "+12"
    assert format_number(Decimal("-0.001"), unit="EUR", signed=True) == "0,00 €"
    assert format_number(math.inf, signed=True) == "+∞"


def test_detect_anomalies_finds_injected_points():
    days = [date(2023 + i // 12, i % 12 + 1, 1) for i in range(36)]
    values = [100 + 4 * math.sin(i) for i in range(36)]
    values[20], values[30] = 160.0, 40.0
    found = detect_anomalies(list(zip(days, values)))
    assert [(a.when, a.direction) for a in found] == [(days[20], "hausse"), (days[30], "baisse")]
    assert found[0].score > 3.5 and found[1].score < -3.5
    assert found[0].expected == pytest.approx(100, abs=5)


def test_detect_anomalies_on_demo_series(engine):
    series = engine.source.query(engine.compile(MONTHLY_CA).sql).rows
    assert len(series) == 24 and not detect_anomalies(series)
    spiked = [(d, v * 4 if i == 15 else v) for i, (d, v) in enumerate(series)]
    found = detect_anomalies(spiked)
    worst = max(found, key=lambda a: abs(a.score))
    assert worst.when == series[15][0] and worst.direction == "hausse"


def test_detect_anomalies_handles_flat_series():
    flat = [(i, 10) for i in range(10)]
    assert detect_anomalies(flat) == []
    found = detect_anomalies(flat + [(10, 11)])
    assert len(found) == 1 and found[0].when == 10 and math.isinf(found[0].score)
    assert detect_anomalies([(0, 1), (1, None), (2, 5)]) == []


def test_threshold_monitors_trigger(engine):
    monitors = [
        Monitor("CA 2025", ThresholdCondition(">", 1_000_000),
                intent=Intent(measures=(Measure(metric="ca"),), time=Y2025)),
        Monitor("CA régional", ThresholdCondition("<", 1_200_000), question="Chiffre d'affaires 2025 par région"),
        Monitor("CA négatif", ThresholdCondition("<", 0), intent=Intent(measures=(Measure(metric="ca"),), time=Y2025)),
    ]
    alerts = run_monitors(engine, monitors)
    assert [a.monitor for a in alerts] == ["CA 2025", "CA régional"]
    total, regional = alerts
    assert total.kind == "seuil" and total.value == _value(engine, monitors[0].intent)
    assert total.message == (f"Moniteur « CA 2025 » : chiffre d'affaires vaut {format_number(total.value, unit='EUR')}, "
                             "au-dessus du seuil de 1 000 000,00 €.")
    assert regional.breaches and all(v < 1_200_000 for _, v in regional.breaches)
    assert all(segment in regional.message for segment, _ in regional.breaches)


def test_anomaly_monitor_flags_incomplete_last_period(engine):
    partial = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="time", grain="month"),),
                     time=TimeScope(start="2024-01-01", end="2025-12-03"))
    alerts = run_monitors(engine, [Monitor("CA mensuel", AnomalyCondition(), intent=partial),
                                   Monitor("CA mensuel complet", AnomalyCondition(), intent=MONTHLY_CA)])
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.kind == "anomalie" and alert.anomalies[0].when == date(2025, 12, 1)
    assert alert.anomalies[0].direction == "baisse"
    assert "1er décembre 2025" in alert.message and "baisse" in alert.message


def test_failing_monitor_becomes_an_error_alert(engine):
    alerts = run_monitors(engine, [
        Monitor("marge", ThresholdCondition("<", 0), question="Quelle est la marge brute par produit ?"),
        Monitor("série", AnomalyCondition(), intent=Intent(measures=(Measure(metric="ca"),), time=Y2025)),
    ])
    assert [a.kind for a in alerts] == ["erreur", "erreur"]
    assert "marge" in alerts[0].message and "série" in alerts[1].message
    with pytest.raises(ValueError):
        Monitor("ni l'un ni l'autre", ThresholdCondition(">", 0))
    with pytest.raises(ValueError):
        Monitor("les deux", ThresholdCondition(">", 0), question="CA", intent=MONTHLY_CA)


def test_notifiers_deliver_alerts(engine):
    posted = []

    class FakeHttp:
        def post(self, url, json, headers):
            posted.append((url, json, headers))
            return SimpleNamespace(raise_for_status=lambda: None)

    class BrokenHttp:
        def post(self, url, json, headers):
            raise ConnectionError("hôte injoignable")

    stream = io.StringIO()
    url = "https://hooks.example.test/alertes"
    notifiers = [WebhookNotifier("https://broken.example.test", client=BrokenHttp()),
                 WebhookNotifier(url, client=FakeHttp(), headers={"X-Jeton": "t"}),
                 ConsoleNotifier(stream)]
    monitor = Monitor("CA 2025", ThresholdCondition(">", 1), intent=Intent(measures=(Measure(metric="ca"),), time=Y2025))
    [alert] = run_monitors(engine, [monitor], notifiers=notifiers)
    assert stream.getvalue().strip() == f"[SEUIL] {alert.message}"
    [(sent_url, payload, headers)] = posted
    assert sent_url == url and headers == {"X-Jeton": "t"}
    assert payload["text"] == alert.message and payload["alert"]["moniteur"] == "CA 2025"
    assert payload["alert"]["valeur"] == float(alert.value)
    assert len(alert.delivery_errors) == 1 and "injoignable" in alert.delivery_errors[0]
