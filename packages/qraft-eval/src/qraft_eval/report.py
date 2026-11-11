"""Synthèse des résultats et rapport Markdown de la baseline."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .baseline import (
    CORRECT,
    INCORRECT,
    RIGHT_ABSTENTION,
    SQL_ERROR,
    SQL_REJECTED,
    WRONG_ANSWER,
    QuestionResult,
)
from .goldset import GoldSet
from .pricing import estimate_cost

QIR_COVERAGE_GATE = 0.85


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f} %"


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def qir_coverage(goldset: GoldSet) -> dict[str, Any]:
    """Couverture QIR v0, d'après les annotations qir_expressible des questions répondables."""
    answerable = [q for q in goldset.questions if q.answerable]
    annotated = [q for q in answerable if q.qir_expressible is not None]
    expressible = sum(1 for q in annotated if q.qir_expressible)
    return {
        "answerable": len(answerable),
        "annotated": len(annotated),
        "expressible": expressible,
        "coverage": _ratio(expressible, len(annotated)),
        "not_expressible": [q.id for q in annotated if not q.qir_expressible],
    }


def _accuracy_by(results: Iterable[QuestionResult], key: Callable[[QuestionResult], Iterable[str]]) -> dict[str, tuple[int, int]]:
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        if not r.expected_answerable:
            continue
        for name in key(r):
            groups[name][1] += 1
            groups[name][0] += r.status == CORRECT
    return {name: (c, t) for name, (c, t) in sorted(groups.items())}


def summarize(results: list[QuestionResult], requested_model: str) -> dict[str, Any]:
    answerable = [r for r in results if r.expected_answerable]
    unanswerable = [r for r in results if not r.expected_answerable]
    correct = sum(r.status == CORRECT for r in answerable)
    attempted = [r for r in answerable if r.status in (CORRECT, INCORRECT, SQL_ERROR, SQL_REJECTED)]
    given = [r for r in results if r.status in (CORRECT, INCORRECT, WRONG_ANSWER)]
    wrong_given = [r for r in given if r.status != CORRECT]
    latencies = [r.latency_s for r in results if r.latency_s > 0]
    tokens = {
        "input": sum(r.input_tokens for r in results),
        "cache_read": sum(r.cache_read_tokens for r in results),
        "cache_write": sum(r.cache_write_tokens for r in results),
        "output": sum(r.output_tokens for r in results),
    }
    cost = estimate_cost(
        requested_model,
        input_tokens=tokens["input"],
        cache_read_tokens=tokens["cache_read"],
        cache_write_tokens=tokens["cache_write"],
        output_tokens=tokens["output"],
    )
    return {
        "questions": len(results),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "correct": correct,
        "execution_accuracy": _ratio(correct, len(answerable)),
        "accuracy_when_attempted": _ratio(correct, len(attempted)),
        "attempted": len(attempted),
        "right_abstentions": sum(r.status == RIGHT_ABSTENTION for r in unanswerable),
        "abstention_accuracy": _ratio(sum(r.status == RIGHT_ABSTENTION for r in unanswerable), len(unanswerable)),
        "answers_given": len(given),
        "wrong_answers_given": len(wrong_given),
        "wrong_answer_rate": _ratio(len(wrong_given), len(given)),
        "statuses": dict(Counter(r.status for r in results)),
        "latency_p50_s": _percentile(latencies, 0.5),
        "latency_p95_s": _percentile(latencies, 0.95),
        "tokens": tokens,
        "estimated_cost_usd": cost,
        "served_by_fallback": [r.id for r in results if r.model and r.model != requested_model],
        "by_difficulty": _accuracy_by(results, lambda r: [r.difficulty or "non renseignée"]),
        "by_tag": _accuracy_by(results, lambda r: r.tags or ["sans tag"]),
    }


def _group_table(title: str, groups: dict[str, tuple[int, int]]) -> list[str]:
    lines = [f"## {title}", "", "| Groupe | Correctes | Précision |", "| --- | --- | --- |"]
    for name, (c, t) in groups.items():
        lines.append(f"| {name} | {c} / {t} | {_pct(_ratio(c, t))} |")
    return lines + [""]


def render_markdown(goldset: GoldSet, results: list[QuestionResult], summary: dict[str, Any], meta: dict[str, Any]) -> str:
    coverage = qir_coverage(goldset)
    p50, p95 = summary["latency_p50_s"], summary["latency_p95_s"]
    latency = "n/a" if p50 is None else f"{p50:.1f} s / {p95:.1f} s"
    cost = summary["estimated_cost_usd"]
    tokens = summary["tokens"]

    if coverage["coverage"] is None:
        qir_status, qir_value = "non annoté", "aucune annotation"
    else:
        qir_status = "atteint" if coverage["coverage"] >= QIR_COVERAGE_GATE else "non atteint"
        qir_value = f"{_pct(coverage['coverage'])} ({coverage['expressible']} / {coverage['annotated']} annotées)"

    lines = [
        f"# Rapport baseline — domaine {goldset.domain}",
        "",
        f"Exécuté le {meta['started_at']} · modèle `{meta['model']}` · effort `{meta['effort']}` · "
        f"dialecte `{goldset.dialect}` · {summary['questions']} questions.",
        "",
        f"Valeurs d'exemple dans le prompt : {'oui' if meta['samples'] else 'non'} · "
        f"repli serveur en cas de refus : {'activé' if meta['fallbacks'] else 'désactivé'}.",
        "",
        "## Résultats principaux",
        "",
        "| Indicateur | Valeur |",
        "| --- | --- |",
        f"| Précision d'exécution (questions répondables) | {_pct(summary['execution_accuracy'])} "
        f"({summary['correct']} / {summary['answerable']}) |",
        f"| Précision quand le modèle tente une réponse | {_pct(summary['accuracy_when_attempted'])} "
        f"({summary['correct']} / {summary['attempted']}) |",
        f"| Abstention correcte (questions sans réponse) | {_pct(summary['abstention_accuracy'])} "
        f"({summary['right_abstentions']} / {summary['unanswerable']}) |",
        f"| Réponses fausses parmi les réponses données | {_pct(summary['wrong_answer_rate'])} "
        f"({summary['wrong_answers_given']} / {summary['answers_given']}) |",
        f"| Latence du modèle p50 / p95 | {latency} |",
        f"| Jetons : entrée / cache lu / cache écrit / sortie | {tokens['input']} / {tokens['cache_read']} / "
        f"{tokens['cache_write']} / {tokens['output']} |",
        f"| Coût estimé | {'n/a' if cost is None else f'{cost:.2f} $'} |",
        "",
        "La baseline n'a pas de score de confiance : toutes ses réponses sont affirmées.",
        "",
        "## Gate 0",
        "",
        "| Critère | Seuil | Mesure | Statut |",
        "| --- | --- | --- | --- |",
        f"| Baseline mesurée sur le jeu or | — | {_pct(summary['execution_accuracy'])} | atteint |",
        f"| Couverture QIR v0 | ≥ 85 % | {qir_value} | {qir_status} |",
        "| Élève pilote | à moins de 10 points de la baseline | non mesuré (spike GPU) | à faire |",
        "",
    ]
    lines += _group_table("Précision par difficulté", summary["by_difficulty"])
    lines += _group_table("Précision par tag", summary["by_tag"])
    lines += ["## Répartition des statuts", "", "| Statut | Nombre |", "| --- | --- |"]
    lines += [f"| {status} | {n} |" for status, n in sorted(summary["statuses"].items())]
    lines.append("")

    if summary["served_by_fallback"]:
        lines += [
            "## Questions servies par un modèle de repli",
            "",
            "Ces questions ont été déclinées puis rejouées sur un autre modèle : elles ne mesurent pas le modèle demandé.",
            "",
            ", ".join(summary["served_by_fallback"]),
            "",
        ]

    failures = [r for r in results if r.status not in (CORRECT, RIGHT_ABSTENTION)]
    lines += ["## Détail des échecs", ""]
    if not failures:
        lines += ["Aucun échec.", ""]
    for r in failures:
        lines += [f"### {r.id} — {r.status}", "", f"**Question** : {r.question}", ""]
        if r.reason:
            lines += [f"**Motif** : {r.reason}", ""]
        if r.assumptions:
            lines += ["**Hypothèses du modèle** : " + " ; ".join(r.assumptions), ""]
        if r.predicted_sql:
            lines += ["```sql", r.predicted_sql, "```", ""]
    return "\n".join(lines)


def write_outputs(
    outdir: Path,
    goldset: GoldSet,
    results: list[QuestionResult],
    summary: dict[str, Any],
    meta: dict[str, Any],
) -> Path:
    """Écrit results.jsonl, summary.json et report.md dans outdir. Retourne le chemin du rapport."""
    outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for r in results:
            handle.write(json.dumps(asdict(r), ensure_ascii=False, default=str) + "\n")
    (outdir / "summary.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "qir_coverage": qir_coverage(goldset)},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    report = outdir / "report.md"
    report.write_text(render_markdown(goldset, results, summary, meta), encoding="utf-8")
    return report
