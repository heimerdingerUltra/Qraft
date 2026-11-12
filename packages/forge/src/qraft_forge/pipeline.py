"""Orchestration de la Forge : échantillonnage -> exécution -> verbalisation -> aller-retour.

forge() enchaîne les étapes 2 à 5 et retourne les paires vérifiées avec un entonnoir de
statistiques ; dataset.write_dataset() les exporte. roundtrip_pairs() repasse au filtre des
paires venues d'ailleurs (paraphrases de Claude, paires réelles de la boucle d'apprentissage).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from qraft.execute import Source
from qraft.graph import KnowledgeGraph

from .checks import ExecutionChecker, Interpreter, Reference, RoundTrip, rule_interpreter
from .dataset import Pair
from .sampler import IntentSampler, Sample
from .verbalize import TemplateVerbalizer


@dataclass
class ForgeConfig:
    n: int = 2000  # intentions vérifiées visées
    seed: int = 42
    per_intent: int = 3  # questions générées par intention
    min_coverage: int = 3  # chaque mesure et dimension au moins N fois
    val_ratio: float = 0.1
    min_parse_coverage: float = 0.75  # seuil de couverture du parseur à règles pour l'aller-retour


@dataclass
class ForgeRun:
    pairs: list[Pair]
    samples: list[Sample]
    references: dict[str, Reference]
    stats: dict[str, Any] = field(default_factory=dict)


def _rate(kept: int, total: int) -> float:
    return round(kept / total, 3) if total else 0.0


def roundtrip_pairs(pairs: list[Pair], checker: ExecutionChecker, interpreter: Interpreter) -> tuple[list[Pair], RoundTrip]:
    """Garde les paires dont la question, ré-interprétée, donne le résultat de leur intention."""
    roundtrip = RoundTrip(checker, interpreter)
    kept = []
    for pair in pairs:
        reference = checker.check(pair.intent)
        if reference is not None and roundtrip.verify(reference, pair.question).kept:
            kept.append(pair)
    return kept, roundtrip


def forge(graph: KnowledgeGraph, source: Source, config: ForgeConfig | None = None, *,
          interpreter: Interpreter | None = None, verbalizer: TemplateVerbalizer | None = None,
          checker: ExecutionChecker | None = None) -> ForgeRun:
    config = config or ForgeConfig()
    checker = checker or ExecutionChecker(graph, source)
    sampler = IntentSampler(graph, seed=config.seed, min_coverage=config.min_coverage)
    references: dict[str, Reference] = {}

    def accept(sample: Sample) -> bool:
        reference = checker.check(sample.intent)
        if reference is not None:
            references[sample.fingerprint] = reference
        return reference is not None

    samples = sampler.sample(config.n, accept=accept)
    verbalizer = verbalizer or TemplateVerbalizer(graph, seed=config.seed)
    roundtrip = RoundTrip(checker, interpreter or rule_interpreter(graph, min_coverage=config.min_parse_coverage))

    pairs: list[Pair] = []
    generated = 0
    silent = 0
    for sample in samples:
        reference = references[sample.fingerprint]
        questions = verbalizer.questions(sample.intent, k=config.per_intent)
        silent += not questions
        for question in questions:
            generated += 1
            if roundtrip.verify(reference, question).kept:
                pairs.append(Pair(question, sample.intent, sample.motif, reference.row_count, "gabarit",
                                  tuple(reference.tables)))

    kept_intents = {p.fingerprint for p in pairs}
    stats = {
        "config": asdict(config),
        "intents": {
            "draws": sampler.attempts,
            "verified": len(samples),
            "rejections": dict(sorted(checker.rejections.items())),
            "without_question": silent,
            "with_pairs": len(kept_intents),
        },
        "questions": {
            "generated": generated,
            "kept": len(pairs),
            "roundtrip_rate": _rate(len(pairs), generated),
            "outcomes": dict(sorted(roundtrip.outcomes.items())),
        },
        "motifs": dict(sorted(Counter(s.motif for s in samples).items())),
        "coverage": sampler.coverage_report(),
        "under_covered": sampler.under_covered(),
    }
    return ForgeRun(pairs, samples, references, stats)
