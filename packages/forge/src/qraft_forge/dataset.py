"""Jeu d'entraînement : export JSONL au format SFT « chat » et découpage sans fuite.

Chaque ligne contient trois messages :
- system : models.INSTRUCTIONS + graph.summary_for_prompt(), le prompt exact du modèle servi ;
- user : la question ;
- assistant : le JSON de l'intention, complet (tous les champs, dans l'ordre du schéma QIR),
  donc conforme au décodage contraint par qir_json_schema(graph) au moment du serving.

Les métadonnées gardent l'empreinte d'intention, le motif, le nombre de lignes de la
référence, l'origine de la question et les tables utilisées (pour invalider les exemples
touchés par un changement de schéma). Le découpage train/validation se fait par empreinte
d'intention : toutes les questions d'une même intention restent du même côté.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from qraft.graph import KnowledgeGraph
from qraft.models import INSTRUCTIONS
from qraft.qir import Intent
from qraft.text import phrase

SPLITS = ("train", "validation")


@dataclass(frozen=True)
class Pair:
    """Une paire question -> intention vérifiée par exécution."""

    question: str
    intent: Intent
    motif: str
    rows: int
    origin: str = "gabarit"  # gabarit | claude | reel
    tables: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        return self.intent.fingerprint()


def system_prompt(graph: KnowledgeGraph) -> str:
    """Le prompt système des modèles servis (ClaudeModel, LocalModel) : la cible d'entraînement doit être la même."""
    return INSTRUCTIONS + "\n" + graph.summary_for_prompt()


def intent_json(intent: Intent) -> str:
    return msgspec.json.encode(intent).decode()


def to_record(pair: Pair, system: str, graph_version: str = "") -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": pair.question},
            {"role": "assistant", "content": intent_json(pair.intent)},
        ],
        "metadata": {
            "fingerprint": pair.fingerprint,
            "motif": pair.motif,
            "rows": pair.rows,
            "origin": pair.origin,
            "tables": list(pair.tables),
            "graph_version": graph_version,
        },
    }


def record_intent(record: dict[str, Any]) -> Intent:
    """Intention cible d'une ligne exportée."""
    return msgspec.json.decode(record["messages"][-1]["content"].encode(), type=Intent)


def dedupe(pairs: list[Pair]) -> list[Pair]:
    """Une question (forme normalisée) n'apparaît qu'une fois dans tout le jeu, avec sa première intention."""
    seen: set[str] = set()
    unique = []
    for pair in pairs:
        key = phrase(pair.question)
        if key and key not in seen:
            seen.add(key)
            unique.append(pair)
    return unique


def split_by_intent(pairs: list[Pair], *, val_ratio: float = 0.1, seed: int = 42) -> dict[str, list[Pair]]:
    """Découpage déterministe par empreinte d'intention : aucune intention n'est des deux côtés."""
    fingerprints = sorted({p.fingerprint for p in pairs},
                          key=lambda fp: hashlib.sha1(f"{seed}:{fp}".encode()).hexdigest())
    n_val = 0
    if val_ratio > 0 and len(fingerprints) > 1:
        n_val = min(len(fingerprints) - 1, max(1, math.ceil(len(fingerprints) * val_ratio)))
    validation = set(fingerprints[:n_val])
    return {
        "train": [p for p in pairs if p.fingerprint not in validation],
        "validation": [p for p in pairs if p.fingerprint in validation],
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_dataset(pairs: list[Pair], out_dir: str | Path, graph: KnowledgeGraph, *, val_ratio: float = 0.1,
                  seed: int = 42, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Écrit train.jsonl, validation.jsonl et manifest.json ; retourne le manifeste."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    unique = dedupe(pairs)
    splits = split_by_intent(unique, val_ratio=val_ratio, seed=seed)
    system = system_prompt(graph)
    for name in SPLITS:
        write_jsonl(out / f"{name}.jsonl", [to_record(p, system, graph.version) for p in splits[name]])
    manifest = {
        "graph_version": graph.version,
        "domain": graph.semantic.domain,
        "seed": seed,
        "val_ratio": val_ratio,
        "pairs": {name: len(splits[name]) for name in SPLITS},
        "intents": {name: len({p.fingerprint for p in splits[name]}) for name in SPLITS},
        "duplicates_removed": len(pairs) - len(unique),
        **(extra or {}),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def dataset_stats(directory: str | Path) -> dict[str, Any]:
    """Statistiques d'un jeu exporté : volumes, motifs, origines, lignes, contrôle de fuite."""
    directory = Path(directory)
    data = {name: read_jsonl(directory / f"{name}.jsonl") for name in SPLITS}
    fingerprints = {name: {r["metadata"]["fingerprint"] for r in rows} for name, rows in data.items()}
    questions = {name: {phrase(r["messages"][1]["content"]) for r in rows} for name, rows in data.items()}
    every = data["train"] + data["validation"]
    rows = [r["metadata"]["rows"] for r in every]
    manifest_path = directory / "manifest.json"
    return {
        "pairs": {name: len(rows_) for name, rows_ in data.items()},
        "intents": {name: len(fps) for name, fps in fingerprints.items()},
        "motifs": dict(sorted(Counter(r["metadata"]["motif"] for r in every).items())),
        "origins": dict(sorted(Counter(r["metadata"]["origin"] for r in every).items())),
        "questions_per_intent": round(len(every) / max(1, len(fingerprints["train"] | fingerprints["validation"])), 2),
        "mean_rows": round(sum(rows) / len(rows), 1) if rows else 0.0,
        "leaked_intents": len(fingerprints["train"] & fingerprints["validation"]),
        "leaked_questions": len(questions["train"] & questions["validation"]),
        "manifest": json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {},
    }
