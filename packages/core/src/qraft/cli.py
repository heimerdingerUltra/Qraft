"""CLI : qraft index | ask | explain | shell | serve."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, help="Qraft : interrogez vos bases SQL en français.", no_args_is_help=True)
console = Console()

DbOption = Annotated[Optional[str], typer.Option("--db", help="URL SQLAlchemy (sinon QRAFT_DB_URL)")]
SemanticOption = Annotated[Optional[Path], typer.Option("--semantic", help="couche sémantique YAML")]
PackOption = Annotated[Optional[Path], typer.Option("--pack", help="pack de connaissance (créé s'il manque)")]
ModelOption = Annotated[str, typer.Option("--model", help="none | claude | local")]


def _engine(db: str | None, semantic: Path | None, pack: Path | None, model: str):
    from .engine import Engine

    url = db or os.environ.get("QRAFT_DB_URL")
    if not url:
        raise typer.BadParameter("indique la base avec --db ou QRAFT_DB_URL")
    engine = Engine.from_url(url, semantic=semantic, pack=pack)
    if model == "claude":
        from .models import ClaudeModel

        engine.model = ClaudeModel(engine.graph, hidden_tables=engine.policy.restricted_tables)
    elif model == "local":
        from .models import LocalModel

        engine.model = LocalModel(engine.graph, base_url=os.environ.get("QRAFT_LOCAL_MODEL_URL", "http://localhost:8000"))
    return engine


def _show(answer, show_sql: bool) -> None:
    style = {"ok": "green", "a_confirmer": "yellow", "clarification": "cyan"}.get(answer.status, "red")
    console.print(f"[bold {style}]{answer.status}[/] · route {answer.route or '-'}"
                  + (f" · confiance {answer.confidence.tier} ({answer.confidence.score:.2f})" if answer.confidence else ""))
    if answer.interpretation:
        console.print(f"[bold]Interprétation[/] : {answer.interpretation}")
    if answer.message:
        console.print(answer.message)
    for alt in answer.alternatives:
        console.print(f"  - {alt}")
    for rule in answer.rules:
        console.print(f"[dim]Règle appliquée : {rule}[/]")
    for note in answer.assumptions + answer.warnings:
        console.print(f"[yellow]! {note}[/]")
    if show_sql and answer.sql:
        console.print(answer.sql, style="dim")
    if answer.columns:
        table = Table(*answer.columns)
        for row in answer.rows[:50]:
            table.add_row(*["" if v is None else str(v) for v in row])
        console.print(table)
        if len(answer.rows) > 50:
            console.print(f"[dim]… {len(answer.rows) - 50} lignes de plus[/]")


@app.command()
def index(db: DbOption = None, semantic: SemanticOption = None,
          out: Annotated[Path, typer.Option("--out")] = Path(".qraft/pack.json")) -> None:
    """Indexe la base : catalogue, profils, clés inférées, couche sémantique."""
    from .engine import build_graph
    from .execute import Source

    url = db or os.environ.get("QRAFT_DB_URL")
    if not url:
        raise typer.BadParameter("indique la base avec --db ou QRAFT_DB_URL")
    graph = build_graph(Source(url), semantic=semantic)
    path = graph.to_pack().save(out)
    inferred = sum(1 for e in graph.edges if e.origin == "inferred")
    console.print(f"Pack écrit : {path} · {len(graph.catalog.tables)} tables · {len(graph.edges)} relations "
                  f"(dont {inferred} inférées) · {len(graph.semantic.metrics)} métriques")


@app.command()
def ask(question: str, db: DbOption = None, semantic: SemanticOption = None, pack: PackOption = None,
        model: ModelOption = "none", sql: Annotated[bool, typer.Option("--sql")] = False,
        as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Pose une question et affiche la réponse."""
    answer = _engine(db, semantic, pack, model).ask(question)
    if as_json:
        console.print_json(json.dumps(answer.to_dict(), ensure_ascii=False, default=str))
    else:
        _show(answer, sql)


@app.command()
def explain(question: str, db: DbOption = None, semantic: SemanticOption = None, pack: PackOption = None,
            model: ModelOption = "none") -> None:
    """Montre l'interprétation et le SQL sans exécuter."""
    _show(_engine(db, semantic, pack, model).ask(question, execute=False), True)


@app.command()
def shell(db: DbOption = None, semantic: SemanticOption = None, pack: PackOption = None,
          model: ModelOption = "none") -> None:
    """Conversation multi-tours ; une question commençant par « et » prolonge la précédente."""
    engine = _engine(db, semantic, pack, model)
    previous = None
    console.print("Qraft — tape une question (vide pour quitter, « /sql » pour voir le dernier SQL).")
    while True:
        try:
            question = console.input("[bold]› [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            break
        if question == "/sql":
            console.print(previous.sql if previous and previous.sql else "aucun SQL", style="dim")
            continue
        answer = engine.ask(question, previous=previous)
        _show(answer, False)
        if answer.intent is not None:
            previous = answer


@app.command()
def serve(db: DbOption = None, semantic: SemanticOption = None, pack: PackOption = None,
          model: ModelOption = "none", host: str = "127.0.0.1", port: int = 8080) -> None:
    """Lance l'API REST/SSE et le serveur MCP (paquet qraft-server)."""
    try:
        from qraft_server.app import run
    except ImportError as error:
        raise typer.BadParameter("installe le paquet qraft-server") from error
    run(_engine(db, semantic, pack, model), host=host, port=port)


def main() -> None:
    app()
