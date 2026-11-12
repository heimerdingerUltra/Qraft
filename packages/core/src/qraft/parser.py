"""Parseur à règles : question française -> QIR, sans modèle.

C'est le chemin rapide (C1 de la spécification) : il couvre les questions qui ne
mobilisent que le vocabulaire du graphe (métriques, dimensions, entités, valeurs),
une période, un classement et des regroupements. Il mesure sa couverture : les
mots qu'il n'explique pas font baisser la confiance, et en dessous d'un seuil la
question part vers un modèle (ou le système s'abstient).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import msgspec

from .graph import KnowledgeGraph, LexiconEntry
from .qir import Dimension, Filter, Intent, Measure, Ranking, Ratio, TimeScope
from .text import NEGATIONS, STOPWORDS, tokens

MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}
ORDINALS = {"premier": 1, "1er": 1, "deuxieme": 2, "second": 2, "seconde": 2, "troisieme": 3, "quatrieme": 4}
GRAIN_WORDS = {"jour": "day", "semaine": "week", "moi": "month", "mois": "month", "trimestre": "quarter",
               "annee": "year", "an": "year"}
GRAIN_ADJECTIVES = {"quotidien": "day", "quotidienne": "day", "hebdomadaire": "week", "mensuel": "month",
                    "mensuelle": "month", "trimestriel": "quarter", "trimestrielle": "quarter",
                    "annuel": "year", "annuelle": "year"}
DIM_CONTEXT = {"par", "chaque", "selon"}
COUNT_CONTEXT = {"combien", "nombre", "nouveau", "nouvelle", "nouveaux", "nouvelles"}
EVOLUTION = {"evolution", "variation", "progression", "croissance", "hausse", "baisse", "comparaison", "compare"}
DESC_WORDS = {"grand", "grande", "fort", "forte", "eleve", "elevee", "haut", "haute", "decroissant", "meilleur",
              "meilleure", "premier", "premiere"}
ASC_WORDS = {"petit", "petite", "faible", "bas", "basse", "croissant", "pire"}
PERCENT = {"pourcent", "pourcentage"}
QUESTION_STARTS = {"quel", "quelle", "quels", "quelles", "liste"}


@dataclass
class ParseResult:
    intent: Intent
    handled: bool
    coverage: float
    unmatched: list[str] = field(default_factory=list)
    reason: str | None = None
    followup: bool = False


@dataclass
class _Match:
    start: int
    end: int
    entries: list[LexiconEntry]

    def kinds(self) -> set[str]:
        return {e.kind for e in self.entries}

    def first(self, kind: str) -> LexiconEntry | None:
        return next((e for e in self.entries if e.kind == kind), None)


def _year(token: str) -> int | None:
    return int(token) if token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2100 else None


def _year_scope(y: int) -> tuple[str, str]:
    return date(y, 1, 1).isoformat(), date(y + 1, 1, 1).isoformat()


def _month_scope(y: int, m: int) -> tuple[str, str]:
    end = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, 1).isoformat(), end.isoformat()


class RuleParser:
    def __init__(self, graph: KnowledgeGraph, today: date | None = None) -> None:
        self.graph = graph
        self.today = today or date.today()
        self.index: dict[tuple[str, ...], list[LexiconEntry]] = {}
        for entry in graph.lexicon():
            self.index.setdefault(tuple(entry.phrase.split()), []).append(entry)
        self.max_len = max((len(k) for k in self.index), default=1)

    # --- Périodes ------------------------------------------------------------------------------

    def _time(self, toks: list[str], used: set[int]) -> tuple[TimeScope | None, bool]:
        """Repère la période ; retourne (période, évolution demandée)."""
        evolution = any(t in EVOLUTION for t in toks)
        n = len(toks)
        for i in range(n - 3):  # entre 2024 et 2025
            y1, y2 = _year(toks[i + 1]), _year(toks[i + 3])
            if toks[i] in ("entre", "de") and y1 and toks[i + 2] in ("et", "a") and y2:
                used.update(range(i, i + 4))
                if evolution and y2 == y1 + 1:
                    s, e = _year_scope(y2)
                    return TimeScope(start=s, end=e, compare="previous_year"), True
                return TimeScope(start=_year_scope(y1)[0], end=_year_scope(y2)[1]), evolution
        for i in range(n - 1):  # janvier 2025, T2 2025
            y = _year(toks[i + 1])
            if y and toks[i] in MONTHS:
                used.update((i, i + 1))
                return TimeScope(*_month_scope(y, MONTHS[toks[i]])), evolution
            if y and len(toks[i]) == 2 and toks[i][0] in "tq" and toks[i][1] in "1234":
                q = int(toks[i][1])
                used.update((i, i + 1))
                return TimeScope(start=date(y, 3 * q - 2, 1).isoformat(),
                                 end=(date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)).isoformat()), evolution
        for i in range(n - 2):  # deuxième trimestre 2025
            y = _year(toks[i + 2])
            if y and toks[i] in ORDINALS and toks[i + 1] == "trimestre":
                q = ORDINALS[toks[i]]
                used.update((i, i + 1, i + 2))
                return TimeScope(start=date(y, 3 * q - 2, 1).isoformat(),
                                 end=(date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)).isoformat()), evolution
        text = " ".join(toks)
        relatives = {
            "depuis le debut de l annee": (date(self.today.year, 1, 1), self.today + timedelta(days=1)),
            "annee derniere": (date(self.today.year - 1, 1, 1), date(self.today.year, 1, 1)),
            "an dernier": (date(self.today.year - 1, 1, 1), date(self.today.year, 1, 1)),
            "annee precedente": (date(self.today.year - 1, 1, 1), date(self.today.year, 1, 1)),
            "cette annee": (date(self.today.year, 1, 1), date(self.today.year + 1, 1, 1)),
            "mois dernier": (
                (self.today.replace(day=1) - timedelta(days=1)).replace(day=1), self.today.replace(day=1)),
            "ce mois": (self.today.replace(day=1),
                        date(self.today.year + (self.today.month == 12), self.today.month % 12 + 1, 1)),
        }
        for expression, (start, end) in relatives.items():
            words = expression.split()
            for i in range(n - len(words) + 1):
                if toks[i:i + len(words)] == words:
                    used.update(range(i, i + len(words)))
                    if evolution:
                        return TimeScope(start=start.isoformat(), end=end.isoformat(), compare="previous_year",
                                         label=expression), True
                    return TimeScope(start=start.isoformat(), end=end.isoformat(), label=expression), evolution
        for i, t in enumerate(toks):  # année seule : « en 2025 », « 2025 »
            if (y := _year(t)) is not None:
                used.add(i)
                s, e = _year_scope(y)
                return TimeScope(start=s, end=e, compare="previous_year" if evolution else None), evolution
        return None, evolution

    # --- Vocabulaire ---------------------------------------------------------------------------

    def _lexical_matches(self, toks: list[str], used: set[int]) -> list[_Match]:
        matches: list[_Match] = []
        taken = set(used)
        candidates = []
        for length in range(min(self.max_len, len(toks)), 0, -1):
            for i in range(len(toks) - length + 1):
                key = tuple(toks[i:i + length])
                if key in self.index:
                    candidates.append((length, i, key))
        for length, i, key in candidates:  # plus long d'abord, puis de gauche à droite
            span = set(range(i, i + length))
            if span & taken:
                continue
            if length == 1 and key[0] in STOPWORDS:
                continue
            taken |= span
            matches.append(_Match(i, i + length, self.index[key]))
        return sorted(matches, key=lambda m: m.start)

    # --- Analyse -------------------------------------------------------------------------------

    def parse(self, question: str, previous: Intent | None = None) -> ParseResult:
        toks = tokens(question)
        used: set[int] = set()
        negated = [t for t in toks if t in NEGATIONS]
        scope, evolution = self._time(toks, used)
        percent = any(t in PERCENT for t in toks)

        # Classement : « top 5 », « les 5 ... les plus », « le plus de ».
        limit, direction, rank_at = None, None, None
        for i, t in enumerate(toks):
            if t.isdigit() and _year(t) is None and int(t) <= 1000:
                window = toks[max(0, i - 1):i + 4]
                if "top" in window or any(w in window for w in ("premier", "premiere", "plus", "meilleur", "moins")):
                    limit, rank_at = int(t), i
                    used.add(i)
                    if i > 0 and toks[i - 1] == "top":
                        used.add(i - 1)
        for i, t in enumerate(toks):
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if t in ("plus", "moins") and nxt:
                if nxt in ASC_WORDS:
                    guess = "asc" if t == "plus" else "desc"
                    used.add(i + 1)
                elif nxt in DESC_WORDS:
                    guess = "desc" if t == "plus" else "asc"
                    used.add(i + 1)
                else:
                    guess = "desc" if t == "plus" else "asc"
                direction = direction or guess  # « de la plus forte à la plus faible » : le premier décide
                used.add(i)
            elif t in ("decroissant", "croissant"):
                direction = "desc" if t == "decroissant" else "asc"
                used.add(i)
            elif t == "ordre" and nxt in ("decroissant", "croissant"):
                used.add(i)

        # Regroupements temporels.
        grains: list[str] = []
        for i, t in enumerate(toks):
            if t in GRAIN_ADJECTIVES:
                grains.append(GRAIN_ADJECTIVES[t])
                used.add(i)
            elif t in GRAIN_WORDS and i > 0 and toks[i - 1] in DIM_CONTEXT and i not in used:
                grains.append(GRAIN_WORDS[t])
                used.update((i - 1, i))

        matches = self._lexical_matches(toks, used)
        measures: list[Measure] = []
        ratios: list[Ratio] = []
        dims: list[Dimension] = []
        filters: list[Filter] = []
        consumed: set[int] = set()

        # Ratio : « part du canal web dans le chiffre d'affaires ».
        if "part" in toks:
            p = toks.index("part")
            dans = next((j for j in range(p + 1, len(toks)) if toks[j] == "dans"), None)
            if dans is not None:
                num_filters = [Filter(column=m.first("value").ref, value=m.first("value").value)
                               for m in matches if p < m.start < dans and m.first("value")]
                metric = next((m for m in matches if m.start > dans and m.first("metric")), None)
                if num_filters and metric:
                    mid = metric.first("metric").ref
                    ratios.append(Ratio(numerator=Measure(metric=mid, filters=tuple(num_filters)),
                                        denominator=Measure(metric=mid), percent=True))
                    for m in matches:
                        if p < m.start < dans or m is metric:
                            consumed.update(range(m.start, m.end))
                    used.update((p, dans))

        starts_question = bool(toks) and toks[0] in QUESTION_STARTS
        for m in matches:
            if m.start in consumed:
                continue
            span = set(range(m.start, m.end))
            before = toks[max(0, m.start - 2):m.start]
            prev = toks[m.start - 1] if m.start > 0 else ""
            in_dim_context = prev in DIM_CONTEXT or (len(before) == 2 and before[0] in DIM_CONTEXT)
            after_rank = rank_at is not None and 0 < m.start - rank_at <= 1
            if (metric := m.first("metric")) is not None:
                measures.append(Measure(metric=metric.ref))
                used |= span
            elif (dim := m.first("dimension")) is not None and (in_dim_context or after_rank or (
                    starts_question and m.start <= 2)):
                dims.append(Dimension(column=dim.ref))
                used |= span
                if in_dim_context:
                    used.update(i for i in range(m.start - 2, m.start) if toks[i] in DIM_CONTEXT)
            elif (entity := m.first("entity")) is not None and any(t in COUNT_CONTEXT for t in before + [prev]):
                measures.append(Measure(agg="count", column=entity.ref))
                used |= span
                used.update(i for i in range(max(0, m.start - 2), m.start) if toks[i] in COUNT_CONTEXT)
            elif (value := m.first("value")) is not None:
                filters.append(Filter(column=value.ref, value=value.value))
                used |= span
            elif m.first("entity") is not None or m.first("dimension") is not None:
                used |= span  # simple mention d'une entité : expliquée, sans effet

        # Couverture : part des mots porteurs de sens que l'analyse explique.
        content = [i for i, t in enumerate(toks) if t not in STOPWORDS and t not in EVOLUTION and t not in PERCENT]
        unmatched = [toks[i] for i in content if i not in used and i not in consumed]
        coverage = 1.0 if not content else 1 - len(unmatched) / len(content)

        followup = previous is not None and not measures and not ratios and (
            bool(toks) and toks[0] == "et" or bool(grains or dims or filters or scope or limit))
        if followup:
            base = previous
            new_dims = tuple(base.dimensions) + tuple(dims) + tuple(Dimension(column="time", grain=g) for g in grains)
            intent = msgspec.structs.replace(
                base,
                dimensions=new_dims,
                filters=tuple(base.filters) + tuple(filters),
                time=scope or base.time,
                ranking=Ranking(direction=direction or "desc", limit=limit) if (limit or direction) else base.ranking,
            )
            return ParseResult(intent, True, coverage, unmatched, followup=True)

        if negated:
            return ParseResult(Intent(answerable=False), False, coverage, unmatched,
                               reason=f"négation (« {negated[0]} ») non exprimable par les règles")
        if not measures and not ratios:
            if dims and not unmatched:
                intent = Intent(dimensions=tuple(dims), filters=tuple(filters), time=scope)
                return ParseResult(intent, True, coverage, unmatched)
            reason = "aucune mesure reconnue" + (f" (mots inconnus : {', '.join(unmatched)})" if unmatched else "")
            return ParseResult(Intent(answerable=False), False, coverage, unmatched, reason=reason)

        if scope and evolution and not scope.compare:
            scope = msgspec.structs.replace(scope, compare="previous_year")
        if scope and scope.compare and percent:
            scope = msgspec.structs.replace(scope, compare_output="pct")
        ranking = None
        if limit or direction:
            if limit is None and direction and toks and toks[0] in ("quel", "quelle") and dims:
                limit = 1
            ranking = Ranking(direction=direction or "desc", limit=limit)
        intent = Intent(
            measures=tuple(dict.fromkeys(measures)),
            ratios=tuple(ratios),
            dimensions=tuple(dict.fromkeys(dims)) + tuple(Dimension(column="time", grain=g) for g in dict.fromkeys(grains)),
            filters=tuple(dict.fromkeys(filters)),
            time=scope,
            ranking=ranking,
        )
        return ParseResult(intent, True, coverage, unmatched)
