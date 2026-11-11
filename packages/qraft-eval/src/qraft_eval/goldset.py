"""Format et chargement des jeux or (fichiers YAML)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Difficulty = Literal["facile", "moyen", "difficile"]


class GoldQuestion(BaseModel):
    """Une question du jeu or et sa réponse de référence."""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    # SQL de référence, validé par un expert métier. Absent si la question n'a pas de réponse.
    sql: str | None = None
    # False : la base ne permet pas de répondre ; le système doit s'abstenir.
    answerable: bool = True
    tags: list[str] = Field(default_factory=list)
    difficulty: Difficulty | None = None
    # Annotation du spike QIR : la question est-elle exprimable en QIR v0 ? None = non annotée.
    qir_expressible: bool | None = None
    # Force une comparaison ordonnée (True) ou non (False). None = déduit du ORDER BY de la référence.
    ordered: bool | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_sql_presence(self) -> GoldQuestion:
        if self.answerable and not (self.sql and self.sql.strip()):
            raise ValueError(f"{self.id} : une question répondable doit avoir un SQL de référence")
        if not self.answerable and self.sql:
            raise ValueError(f"{self.id} : une question sans réponse ne doit pas avoir de SQL")
        return self


class GoldSet(BaseModel):
    """Un jeu or : un domaine, une base, des questions."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    # Dialecte SQLGlot de la base : duckdb, postgres, snowflake, tsql, oracle, mysql, bigquery...
    dialect: str
    description: str | None = None
    questions: list[GoldQuestion]

    @model_validator(mode="after")
    def _check_unique_ids(self) -> GoldSet:
        duplicates = [qid for qid, n in Counter(q.id for q in self.questions).items() if n > 1]
        if duplicates:
            raise ValueError(f"identifiants en double : {', '.join(sorted(duplicates))}")
        return self


def load_goldset(path: str | Path) -> GoldSet:
    """Charge et valide un jeu or YAML. Lève une erreur explicite s'il est mal formé."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return GoldSet.model_validate(data)
