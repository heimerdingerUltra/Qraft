"""CLI : qraft-forge generate | stats."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Forge Qraft : compile une base en jeu d'entraînement question -> QIR vérifié par exécution.")
console = Console()


@app.command()
def generate(
    db: Annotated[Optional[str], typer.Option("--db", help="URL SQLAlchemy (sinon QRAFT_DB_URL)")] = None,
    semantic: Annotated[Optional[Path], typer.Option("--semantic", help="couche sémantique YAML")] = None,
    out: Annotated[Path, typer.Option("--out", help="dossier de sortie")] = Path("forge"),
    n: Annotated[int, typer.Option("--n", help="intentions vérifiées visées")] = 2000,
    seed: Annotated[int, typer.Option("--seed")] = 42,
    per_intent: Annotated[int, typer.Option("--per-intent", help="questions générées par intention")] = 3,
    min_coverage: Annotated[int, typer.Option("--min-coverage", help="chaque mesure et dimension au moins N fois")] = 3,
    val_ratio: Annotated[float, typer.Option("--val-ratio", help="part des intentions en validation")] = 0.1,
    paraphrase: Annotated[bool, typer.Option(
        "--paraphrase-requests", help="écrit aussi les requêtes de paraphrase Claude (lot Message Batches), sans les envoyer"
    )] = False,
) -> None:
    """Échantillonne, exécute, verbalise, vérifie par aller-retour et exporte le jeu SFT."""
    from qraft.engine import build_graph
    from qraft.execute import Source

    from .dataset import write_dataset
    from .pipeline import ForgeConfig, forge

    url = db or os.environ.get("QRAFT_DB_URL")
    if not url:
        raise typer.BadParameter("indique la base avec --db ou QRAFT_DB_URL")
    source = Source(url)
    graph = build_graph(source, semantic=semantic)
    config = ForgeConfig(n=n, seed=seed, per_intent=per_intent, min_coverage=min_coverage, val_ratio=val_ratio)
    run = forge(graph, source, config)
    manifest = write_dataset(run.pairs, out, graph, val_ratio=val_ratio, seed=seed, extra={"funnel": run.stats})
    if paraphrase:
        from .verbalize import ClaudeParaphraser

        paraphraser = ClaudeParaphraser(graph)  # aucun client créé : rien n'est envoyé
        requests = paraphraser.build_requests(run.pairs)
        with (out / "paraphrase_requests.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for request in requests:
                handle.write(json.dumps(request, ensure_ascii=False) + "\n")
        paraphraser.save_index(out / "paraphrase_index.jsonl")
        console.print(f"Requêtes de paraphrase : {len(requests)} (paraphrase_requests.jsonl, non envoyées)")

    funnel = run.stats
    console.print(f"Intentions : {funnel['intents']['verified']} vérifiées sur {funnel['intents']['draws']} tirages "
                  f"· rejets {funnel['intents']['rejections']}")
    console.print(f"Questions : {funnel['questions']['kept']} gardées sur {funnel['questions']['generated']} "
                  f"(aller-retour {funnel['questions']['roundtrip_rate']:.0%})")
    console.print(f"Jeu écrit dans {out} : {manifest['pairs']['train']} paires d'entraînement, "
                  f"{manifest['pairs']['validation']} de validation")
    if funnel["under_covered"]:
        console.print(f"[yellow]! couverture inférieure à {min_coverage} : {', '.join(funnel['under_covered'])}[/]")


@app.command()
def stats(directory: Annotated[Path, typer.Argument(help="dossier produit par generate")]) -> None:
    """Statistiques d'un jeu exporté : volumes, motifs, origines, couverture, contrôle de fuite."""
    from .dataset import dataset_stats

    if not directory.exists():
        raise typer.BadParameter(f"dossier introuvable : {directory}")
    report = dataset_stats(directory)
    table = Table("Découpage", "Paires", "Intentions")
    for split in ("train", "validation"):
        table.add_row(split, str(report["pairs"][split]), str(report["intents"][split]))
    console.print(table)
    console.print(f"Questions par intention : {report['questions_per_intent']} · lignes moyennes par résultat : "
                  f"{report['mean_rows']}")
    console.print("Motifs : " + ", ".join(f"{k} {v}" for k, v in report["motifs"].items()))
    console.print("Origines : " + ", ".join(f"{k} {v}" for k, v in report["origins"].items()))
    funnel = report["manifest"].get("funnel", {})
    if funnel:
        q = funnel["questions"]
        console.print(f"Aller-retour : {q['kept']}/{q['generated']} ({q['roundtrip_rate']:.0%}) · issues {q['outcomes']}")
        coverage = funnel.get("coverage", {})
        if coverage:
            console.print("Couverture : " + ", ".join(f"{k} {v}" for k, v in coverage.items()))
    leaks = report["leaked_intents"] + report["leaked_questions"]
    style = "green" if leaks == 0 else "red"
    console.print(f"[{style}]Fuite train/validation : {report['leaked_intents']} intention(s), "
                  f"{report['leaked_questions']} question(s)[/]")
    if leaks:
        raise typer.Exit(code=1)


def main() -> None:
    app()
