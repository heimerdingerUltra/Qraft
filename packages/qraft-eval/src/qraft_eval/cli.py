"""Interface en ligne de commande : qraft-eval <commande>."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from .goldset import load_goldset

# Hypothèses de l'estimation de coût à blanc (jetons de sortie par question, réflexion comprise).
OUTPUT_TOKENS_GUESS = {"bas": 600, "médian": 1500, "haut": 4000}
CHARS_PER_TOKEN = 3.5


def _db_url(args: argparse.Namespace) -> str:
    url = args.db_url or os.environ.get("QRAFT_DB_URL")
    if not url:
        raise SystemExit("Indique la base avec --db-url ou la variable d'environnement QRAFT_DB_URL.")
    return url


def cmd_demo_db(args: argparse.Namespace) -> int:
    from .demo import build_demo_db

    path = build_demo_db(Path(args.out)).resolve()
    print(f"Base de démonstration créée : {path}")
    print(f"URL SQLAlchemy : duckdb:///{path.as_posix()}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .db import make_engine
    from .validate import ERROR, validate_goldset

    goldset = load_goldset(args.goldset)
    issues, row_counts = validate_goldset(goldset, make_engine(_db_url(args)), max_rows=args.max_rows)
    answerable = sum(q.answerable for q in goldset.questions)
    print(f"Jeu or « {goldset.domain} » : {len(goldset.questions)} questions, dont {answerable} répondables.")
    print(f"Références exécutées avec succès : {len(row_counts)} / {answerable}.")
    for issue in issues:
        print(f"  [{issue.level}] {issue.question_id} : {issue.message}")
    errors = sum(issue.level == ERROR for issue in issues)
    print("Validation réussie." if errors == 0 else f"{errors} erreur(s) à corriger.")
    return 0 if errors == 0 else 1


def cmd_schema(args: argparse.Namespace) -> int:
    from .db import make_engine
    from .schema import describe_schema

    print(describe_schema(make_engine(_db_url(args)), schema=args.schema, include_samples=args.samples))
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from .report import QIR_COVERAGE_GATE, qir_coverage

    cov = qir_coverage(load_goldset(args.goldset))
    print(f"Questions répondables : {cov['answerable']} · annotées : {cov['annotated']}.")
    if cov["coverage"] is None:
        print("Aucune annotation qir_expressible : couverture non mesurable.")
        return 1
    verdict = "atteinte" if cov["coverage"] >= QIR_COVERAGE_GATE else "non atteinte"
    print(f"Couverture QIR v0 : {100 * cov['coverage']:.1f} % — gate de 85 % {verdict}.")
    if cov["not_expressible"]:
        print("Non exprimables : " + ", ".join(cov["not_expressible"]))
    return 0


def _print_estimate(prompt: str, n_questions: int, model: str) -> None:
    from .pricing import PRICES_PER_MTOK

    prefix = len(prompt) / CHARS_PER_TOKEN
    prices = PRICES_PER_MTOK.get(model)
    print(f"Questions : {n_questions} · préfixe (consignes + schéma) : ~{prefix:,.0f} jetons.".replace(",", " "))
    if prices is None:
        print(f"Pas de tarif connu pour {model} : estimation impossible.")
        return
    question_tokens = 60
    for label, out in OUTPUT_TOKENS_GUESS.items():
        cached = (prefix * prices["cache_write_1h"] + (n_questions - 1) * prefix * prices["cache_read"]
                  + n_questions * (question_tokens * prices["input"] + out * prices["output"])) / 1e6
        uncached = n_questions * ((prefix + question_tokens) * prices["input"] + out * prices["output"]) / 1e6
        print(f"  hypothèse {label} ({out} jetons de sortie/question) : ~{cached:.2f} $ avec cache, "
              f"~{uncached:.2f} $ sans cache")
    print("Le cache ne s'applique qu'au-delà d'une taille minimale de préfixe, qui dépend du modèle.")


def cmd_baseline(args: argparse.Namespace) -> int:
    from .baseline import ClaudeGenerator, build_system_prompt, run_baseline
    from .db import make_engine
    from .report import summarize, write_outputs
    from .schema import describe_schema

    goldset = load_goldset(args.goldset)
    engine = make_engine(_db_url(args))
    prompt = build_system_prompt(describe_schema(engine, schema=args.schema, include_samples=args.samples),
                                 goldset.dialect)
    n_questions = min(args.limit, len(goldset.questions)) if args.limit else len(goldset.questions)

    if args.dry_run:
        print("Simulation : aucun appel au modèle.")
        _print_estimate(prompt, n_questions, args.model)
        return 0

    started_at = datetime.now()
    generator = ClaudeGenerator(prompt, model=args.model, effort=args.effort, fallbacks=not args.no_fallbacks)
    print(f"Baseline : {n_questions} questions, modèle {args.model}, effort {args.effort}.")
    results = run_baseline(
        goldset, engine, generator, concurrency=args.concurrency, limit=args.limit, max_rows=args.max_rows,
        on_progress=lambda done, total, qid: print(f"  {done}/{total} {qid}", flush=True),
    )
    summary = summarize(results, args.model)
    meta = {
        "started_at": started_at.strftime("%Y-%m-%d %H:%M"),
        "model": args.model,
        "effort": args.effort,
        "samples": args.samples,
        "fallbacks": not args.no_fallbacks,
        "goldset": str(args.goldset),
    }
    outdir = Path(args.out) / f"{goldset.domain}-{started_at:%Y%m%d-%H%M%S}"
    report = write_outputs(outdir, goldset, results, summary, meta)
    accuracy = summary["execution_accuracy"]
    print(f"Précision d'exécution : {'n/a' if accuracy is None else f'{100 * accuracy:.1f} %'} "
          f"({summary['correct']} / {summary['answerable']}).")
    print(f"Rapport : {report.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qraft-eval", description="Harnais d'évaluation de Qraft (phase 0).")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo-db", help="crée la base de démonstration DuckDB")
    demo.add_argument("--out", default="phase0/demo/ventes.duckdb")
    demo.set_defaults(func=cmd_demo_db)

    def add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db-url", help="URL SQLAlchemy de la base (sinon QRAFT_DB_URL)")
        p.add_argument("--max-rows", type=int, default=100_000)

    validate = sub.add_parser("validate", help="vérifie que chaque référence du jeu or s'exécute")
    validate.add_argument("goldset", type=Path)
    add_db(validate)
    validate.set_defaults(func=cmd_validate)

    schema = sub.add_parser("schema", help="affiche le schéma tel qu'il sera envoyé au modèle")
    add_db(schema)
    schema.add_argument("--schema", help="schéma de base à décrire (par défaut : celui de la connexion)")
    schema.add_argument("--samples", action="store_true", help="ajoute les valeurs fréquentes des colonnes texte")
    schema.set_defaults(func=cmd_schema)

    coverage = sub.add_parser("coverage", help="couverture QIR v0 d'après les annotations du jeu or")
    coverage.add_argument("goldset", type=Path)
    coverage.set_defaults(func=cmd_coverage)

    baseline = sub.add_parser("baseline", help="mesure la baseline : grand modèle + schéma complet")
    baseline.add_argument("goldset", type=Path)
    add_db(baseline)
    baseline.add_argument("--schema", help="schéma de base à décrire")
    baseline.add_argument("--samples", action="store_true",
                          help="envoie au modèle les valeurs fréquentes des colonnes texte")
    baseline.add_argument("--model", default="claude-opus-5-5")
    baseline.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    baseline.add_argument("--no-fallbacks", action="store_true", help="désactive le repli serveur en cas de refus")
    baseline.add_argument("--concurrency", type=int, default=4)
    baseline.add_argument("--limit", type=int, help="n'évalue que les N premières questions")
    baseline.add_argument("--out", default="phase0/results")
    baseline.add_argument("--dry-run", action="store_true", help="estime le coût sans appeler le modèle")
    baseline.set_defaults(func=cmd_baseline)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
