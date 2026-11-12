"""Échantillonnage d'intentions : tirer des QIR valides en parcourant le graphe (étape 2 de la Forge).

On combine des mesures (métriques certifiées ou comptages d'entités), des dimensions
(glossaire et grains temporels), des filtres de valeurs (valeurs profilées des colonnes
texte), des périodes (bornes min/max des colonnes temporelles), des classements top-k,
des comparaisons N-1 et des ratios.

Le tirage est déterministe (graine) et pondéré par la couverture : tant qu'une mesure ou
une dimension n'a pas été retenue N fois, son poids est multiplié. Les combinaisons
incompatibles (aucun chemin de jointure, piège de grain, chemin ambigu, période ignorée)
sont écartées à l'avance par une compilation à blanc, sans toucher à la base.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from qraft.compiler import Compiler
from qraft.graph import KnowledgeGraph
from qraft.qir import Dimension, Filter, Intent, Measure, Ranking, Ratio, TimeScope

# Motifs d'intention et poids de tirage (avant correction par la couverture).
MOTIFS: dict[str, float] = {
    "mesure": 0.10,
    "mesure_dim": 0.18,
    "serie": 0.14,
    "top_k": 0.13,
    "top_1": 0.07,
    "tri": 0.05,
    "comparaison": 0.13,
    "ratio": 0.11,
    "multi_mesures": 0.09,
}
# Grains temporels et poids de base ; périodes compatibles avec chaque grain (None : toute la plage).
GRAINS: dict[str, float] = {"month": 1.0, "quarter": 1.0, "year": 0.8, "week": 0.5, "day": 0.3}
GRAIN_PERIODS: dict[str, tuple[str | None, ...]] = {
    "day": ("month",),
    "week": ("month", "quarter"),
    "month": ("year", "quarter", None),
    "quarter": ("year", None),
    "year": (None, "range"),
}
TOP_K = (3, 5, 10)
COVERAGE_BOOST = 4.0


@dataclass(frozen=True)
class MeasureSpec:
    """Une mesure tirable et ce qui lui est compatible dans le graphe."""

    key: str  # « metric:ca » ou « count:clients »
    measure: Measure
    additive: bool  # somme ou comptage : « part de ... dans ... » a un sens
    time_column: str | None
    start: date | None  # première date des données
    end: date | None  # borne exclusive : lendemain de la dernière date
    dims: tuple[str, ...]  # dimensions du glossaire compatibles
    filters: tuple[str, ...]  # colonnes filtrables compatibles, « table.colonne »


@dataclass(frozen=True)
class Sample:
    intent: Intent
    motif: str
    fingerprint: str


def measure_key(m: Measure) -> str:
    if m.metric:
        return f"metric:{m.metric}"
    if m.agg == "count" and m.column and "." not in m.column:
        return f"count:{m.column}"
    return f"{m.agg}:{m.column}"


def intent_keys(intent: Intent) -> list[str]:
    """Nœuds du graphe qu'une intention mobilise (mesures et dimensions), pour la couverture."""
    keys = [measure_key(m) for m in intent.measures] + [measure_key(r.numerator) for r in intent.ratios]
    keys += [f"time:{d.grain}" if d.column == "time" else f"dim:{d.column}" for d in intent.dimensions]
    return list(dict.fromkeys(keys))


def _parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _year(y: int) -> tuple[date, date]:
    return date(y, 1, 1), date(y + 1, 1, 1)


def _quarter(y: int, q: int) -> tuple[date, date]:
    return date(y, 3 * q - 2, 1), date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)


def _month(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)


def _covered(start: date, end: date, lo: date, hi: date, ratio: float = 0.8) -> bool:
    """La période [start, end[ est couverte à au moins ratio par les données [lo, hi[."""
    overlap = (min(end, hi) - max(start, lo)).days
    return overlap >= ratio * (end - start).days


def _shift_year(d: date) -> date:
    return d.replace(year=d.year - 1)  # les périodes tirées commencent toujours un 1er du mois


class IntentSampler:
    """Tire des intentions valides et variées pour un domaine."""

    def __init__(self, graph: KnowledgeGraph, *, seed: int = 42, min_coverage: int = 3,
                 max_filter_values: int = 30, compiler: Compiler | None = None) -> None:
        self.graph = graph
        self.rng = random.Random(seed)
        self.min_coverage = min_coverage
        self.max_filter_values = max_filter_values
        self.compiler = compiler or Compiler(graph)
        self.filter_values = self._filter_values()
        self.dimensions = [d for d, dim in graph.semantic.dimensions.items() if graph.catalog.resolve(dim.column)]
        self.specs = self._measure_specs()
        self.coverage: Counter[str] = Counter()
        self.attempts = 0

    # --- Préparation -------------------------------------------------------------------------

    def _filter_values(self) -> dict[str, list[str]]:
        """Colonnes texte à faible cardinalité et leurs valeurs profilées."""
        found: dict[str, list[str]] = {}
        for tbl in self.graph.catalog.tables.values():
            for col in tbl.columns:
                if col.is_text and 2 <= len(col.values) <= self.max_filter_values \
                        and (col.distinct or 0) <= self.max_filter_values:
                    found[f"{tbl.name}.{col.name}"] = list(col.values)
        return found

    def _compiles(self, intent: Intent, *, allow_fanout: bool = False) -> bool:
        try:
            compiled = self.compiler.compile(intent)
        except Exception:  # noqa: BLE001 - combinaison incompatible : simplement écartée
            return False
        if compiled.warnings or compiled.signals.get("ambiguous_join"):
            return False
        return allow_fanout or not compiled.signals.get("fanout")

    def _fanout_safe(self, m: Measure) -> bool:
        """Un comptage d'entités devient COUNT(DISTINCT clé) sous un piège de grain : il reste juste."""
        if m.metric or m.agg != "count" or not m.column or "." in m.column:
            return False
        tbl = self.graph.catalog.table(m.column)
        return tbl is not None and len(tbl.primary_key) == 1

    def _identifies(self, column: str, measure: Measure) -> bool:
        """Compter les lignes d'une table par l'un de ses identifiants (regroupement ou filtre) donne 1 : sans intérêt."""
        if measure.metric or not measure.column:
            return False
        tbl, col = self.graph.column(column)
        return tbl.name == measure.column and bool(tbl.rows) and (col.distinct or 0) >= (tbl.rows or 0)

    def _depends(self, dim_id: str, column: str) -> bool:
        """La dimension détermine probablement la colonne (même table, au moins autant de valeurs) :
        la part de cette valeur par dimension ne vaudrait que 0 ou 100 %."""
        dim_tbl, dim_col = self.graph.column(self._dim_column(dim_id))
        tbl, col = self.graph.column(column)
        return dim_tbl.name == tbl.name and (dim_col.distinct or 0) >= (col.distinct or 0)

    def _measure_specs(self) -> list[MeasureSpec]:
        candidates: list[tuple[str, Measure, bool]] = []
        for metric in sorted(self.graph.semantic.metrics.values(), key=lambda m: m.id):
            expression = metric.expression.strip().upper()
            additive = not metric.is_composite and expression.startswith(("SUM(", "COUNT("))
            candidates.append((f"metric:{metric.id}", Measure(metric=metric.id), additive))
        entities = [t for t in self.graph.semantic.entities if self.graph.catalog.table(t)] \
            or list(self.graph.catalog.tables)
        for table in sorted(entities):
            name = self.graph.catalog.table(table).name
            candidates.append((f"count:{name}", Measure(agg="count", column=name), True))

        specs = []
        for key, measure, additive in candidates:
            if not self._compiles(Intent(measures=(measure,))):
                continue
            fanout_ok = self._fanout_safe(measure)
            time_column, start, end = self._time_bounds(measure)
            intrinsic = " ".join(self.graph.metric(measure.metric).filters).lower() if measure.metric else ""
            dims = tuple(d for d in self.dimensions
                         if not self._identifies(self._dim_column(d), measure)
                         and self._compiles(Intent(measures=(measure,), dimensions=(Dimension(column=d),)),
                                            allow_fanout=fanout_ok))
            filters = tuple(
                col for col, values in self.filter_values.items()
                if col.lower() not in intrinsic  # filtre redondant ou contradictoire avec la métrique
                and not self._identifies(col, measure)
                and self._compiles(Intent(measures=(measure,), filters=(Filter(column=col, value=values[0]),)),
                                   allow_fanout=fanout_ok))
            specs.append(MeasureSpec(key, measure, additive, time_column, start, end, dims, filters))
        return specs

    def _time_bounds(self, measure: Measure) -> tuple[str | None, date | None, date | None]:
        try:
            atoms, _ = self.compiler.measure_atoms(measure)
        except Exception:  # noqa: BLE001
            return None, None, None
        columns = {a.time_column for a in atoms}
        if len(columns) != 1 or None in columns:
            return None, None, None
        time_column = columns.pop()
        _, col = self.graph.column(time_column)
        lo, hi = _parse_day(col.min), _parse_day(col.max)
        if lo is None or hi is None:
            return None, None, None
        return time_column, lo, hi + timedelta(days=1)

    # --- Tirage pondéré ------------------------------------------------------------------------

    def _weight(self, key: str, base: float = 1.0) -> float:
        count = self.coverage[key]
        return base * (COVERAGE_BOOST if count < self.min_coverage else 1.0) / (1 + count)

    def _weighted(self, items: Sequence[Any], key: Callable[[Any], str],
                  base: Callable[[Any], float] | None = None) -> Any:
        weights = [self._weight(key(i), base(i) if base else 1.0) for i in items]
        return self.rng.choices(list(items), weights=weights)[0]

    def _maybe(self, p: float) -> bool:
        return self.rng.random() < p

    def _spec(self, condition: Callable[[MeasureSpec], bool], exclude: str | None = None) -> MeasureSpec | None:
        eligible = [s for s in self.specs if condition(s) and s.key != exclude]
        return self._weighted(eligible, key=lambda s: s.key) if eligible else None

    def _dim(self, dims: Sequence[str], exclude_column: str | None = None) -> str | None:
        eligible = [d for d in dims if exclude_column is None or self._dim_column(d) != exclude_column]
        return self._weighted(eligible, key=lambda d: f"dim:{d}") if eligible else None

    def _dim_column(self, dim_id: str) -> str:
        return self.graph.dimension(dim_id)[0]

    def _filter(self, spec: MeasureSpec, exclude_column: str | None = None) -> Filter | None:
        columns = [c for c in spec.filters if c != exclude_column]
        if not columns:
            return None
        column = self.rng.choice(columns)
        return Filter(column=column, value=self.rng.choice(self.filter_values[column]))

    def _periods(self, spec: MeasureSpec, kind: str, compare: bool) -> list[tuple[date, date]]:
        lo, hi = spec.start, spec.end
        if lo is None or hi is None:
            return []
        years = range(lo.year, hi.year + 1)
        if kind == "year":
            spans = [_year(y) for y in years]
        elif kind == "quarter":
            spans = [_quarter(y, q) for y in years for q in range(1, 5)]
        elif kind == "month":
            spans = [_month(y, m) for y in years for m in range(1, 13)]
        else:  # « range » : deux années civiles consécutives
            spans = [(date(y, 1, 1), date(y + 2, 1, 1)) for y in years]
        return [(s, e) for s, e in spans if _covered(s, e, lo, hi)
                and (not compare or _covered(_shift_year(s), _shift_year(e), lo, hi))]

    def _period(self, spec: MeasureSpec, kinds: Sequence[str], *, compare: bool = False) -> TimeScope | None:
        options = [(k, spans) for k in kinds if (spans := self._periods(spec, k, compare))]
        if not options:
            return None
        _, spans = self.rng.choice(options)
        start, end = self.rng.choice(spans)
        if not compare:
            return TimeScope(start=start.isoformat(), end=end.isoformat())
        output = "pct" if self._maybe(0.35) else "all"
        return TimeScope(start=start.isoformat(), end=end.isoformat(), compare="previous_year", compare_output=output)

    # --- Motifs ----------------------------------------------------------------------------------

    def _optional_period(self, spec: MeasureSpec, p: float) -> TimeScope | None:
        if spec.time_column and self._maybe(p):
            return self._period(spec, ("year", "quarter", "month", "range"))
        return None

    def _build_mesure(self) -> Intent | None:
        spec = self._spec(lambda s: True)
        if spec is None:
            return None
        f = self._filter(spec) if self._maybe(0.5) else None
        return Intent(measures=(spec.measure,), filters=(f,) if f else (), time=self._optional_period(spec, 0.75))

    def _build_mesure_dim(self) -> Intent | None:
        spec = self._spec(lambda s: bool(s.dims))
        if spec is None:
            return None
        dim = self._dim(spec.dims)
        f = self._filter(spec, self._dim_column(dim)) if self._maybe(0.35) else None
        return Intent(measures=(spec.measure,), dimensions=(Dimension(column=dim),), filters=(f,) if f else (),
                      time=self._optional_period(spec, 0.6))

    def _build_serie(self) -> Intent | None:
        spec = self._spec(lambda s: s.time_column is not None)
        if spec is None:
            return None
        grain = self._weighted(list(GRAINS), key=lambda g: f"time:{g}", base=lambda g: GRAINS[g])
        kind = self.rng.choice(GRAIN_PERIODS[grain])
        time = self._period(spec, (kind,)) if kind else None
        if kind and time is None:
            return None
        dims = [Dimension(column="time", grain=grain)]
        exclude = None
        if spec.dims and grain in ("month", "quarter", "year") and self._maybe(0.2):
            dim = self._dim(spec.dims)
            dims.insert(0, Dimension(column=dim))  # ordre du parseur : glossaire puis temps
            exclude = self._dim_column(dim)
        f = self._filter(spec, exclude) if self._maybe(0.25) else None
        return Intent(measures=(spec.measure,), dimensions=tuple(dims), filters=(f,) if f else (), time=time)

    def _ranked(self, limit: int | None, p_period: float) -> Intent | None:
        spec = self._spec(lambda s: bool(s.dims))
        if spec is None:
            return None
        dim = self._dim(spec.dims)
        if limit is not None and limit > 1:
            # Un top k n'a de sens que si la dimension a plus de k valeurs.
            size = self.graph.column(self._dim_column(dim))[1].distinct or 0
            choices = [k for k in TOP_K if k < size] or ([2] if size > 2 else [])
            if not choices:
                return None
            limit = self.rng.choice(choices)
        direction = "desc" if self._maybe(0.75) else "asc"
        f = self._filter(spec, self._dim_column(dim)) if self._maybe(0.2) else None
        return Intent(measures=(spec.measure,), dimensions=(Dimension(column=dim),), filters=(f,) if f else (),
                      time=self._optional_period(spec, p_period), ranking=Ranking(direction=direction, limit=limit))

    def _build_top_k(self) -> Intent | None:
        return self._ranked(TOP_K[0], 0.6)  # k choisi selon le nombre de valeurs de la dimension

    def _build_top_1(self) -> Intent | None:
        return self._ranked(1, 0.6)

    def _build_tri(self) -> Intent | None:
        return self._ranked(None, 0.5)

    def _build_comparaison(self) -> Intent | None:
        spec = self._spec(lambda s: s.time_column is not None)
        if spec is None:
            return None
        time = self._period(spec, ("year", "quarter", "month"), compare=True)
        if time is None:
            return None
        dim = self._dim(spec.dims) if spec.dims and self._maybe(0.4) else None
        f = self._filter(spec, self._dim_column(dim) if dim else None) if self._maybe(0.2) else None
        return Intent(measures=(spec.measure,), dimensions=(Dimension(column=dim),) if dim else (),
                      filters=(f,) if f else (), time=time)

    def _build_ratio(self) -> Intent | None:
        spec = self._spec(lambda s: s.additive and s.measure.metric is not None and bool(s.filters))
        if spec is None:
            return None
        part = self._filter(spec)
        if part is None:
            return None
        numerator = Measure(metric=spec.measure.metric, filters=(part,))
        ratio = Ratio(numerator=numerator, denominator=Measure(metric=spec.measure.metric), percent=True)
        dims = [d for d in spec.dims if not self._depends(d, part.column)]
        dim = self._dim(dims) if dims and self._maybe(0.35) else None
        return Intent(ratios=(ratio,), dimensions=(Dimension(column=dim),) if dim else (),
                      time=self._optional_period(spec, 0.5))

    def _build_multi_mesures(self) -> Intent | None:
        first = self._spec(lambda s: s.measure.metric is not None)
        if first is None:
            return None
        second = self._spec(lambda s: s.measure.metric is not None, exclude=first.key)
        if second is None:
            return None
        common = [d for d in first.dims if d in second.dims]
        dim = self._dim(common) if common and self._maybe(0.7) else None
        time = self._optional_period(first, 0.6) if second.time_column else None
        return Intent(measures=(first.measure, second.measure),
                      dimensions=(Dimension(column=dim),) if dim else (), time=time)

    # --- API ------------------------------------------------------------------------------------

    def _motif_weights(self) -> list[float]:
        """Les motifs capables de couvrir un grain temporel ou une dimension encore rares sont favorisés."""
        weights = dict(MOTIFS)
        if any(s.time_column for s in self.specs) and any(self.coverage[f"time:{g}"] < self.min_coverage
                                                           for g in GRAINS):
            weights["serie"] *= COVERAGE_BOOST
        if any(self.coverage[f"dim:{d}"] < self.min_coverage for d in self.dimensions):
            weights["mesure_dim"] *= 2.0
        return list(weights.values())

    def draw(self) -> Sample | None:
        """Tire une intention candidate (non vérifiée), ou None si le motif tiré est infaisable."""
        motif = self.rng.choices(list(MOTIFS), weights=self._motif_weights())[0]
        intent = getattr(self, f"_build_{motif}")()
        return None if intent is None else Sample(intent, motif, intent.fingerprint())

    def sample(self, n: int, accept: Callable[[Sample], bool] | None = None, *,
               max_attempts: int | None = None) -> list[Sample]:
        """Tire jusqu'à n intentions distinctes (par empreinte) acceptées par accept.

        accept sert typiquement à exécuter l'intention ; seules les intentions acceptées
        comptent pour la couverture, ce qui pousse le tirage vers les nœuds encore rares.
        """
        accepted: list[Sample] = []
        seen: set[str] = set()
        limit = self.attempts + (max_attempts or max(200, 40 * n))
        while len(accepted) < n and self.attempts < limit:
            self.attempts += 1
            sample = self.draw()
            if sample is None or sample.fingerprint in seen:
                continue
            seen.add(sample.fingerprint)
            if accept is not None and not accept(sample):
                continue
            accepted.append(sample)
            self.coverage.update(intent_keys(sample.intent))
        return accepted

    def coverage_report(self) -> dict[str, int]:
        """Nombre d'intentions retenues par mesure, dimension et grain temporel (0 compris)."""
        keys = [s.key for s in self.specs]
        keys += [f"dim:{d}" for d in self.dimensions]
        if any(s.time_column for s in self.specs):
            keys += [f"time:{g}" for g in GRAINS]
        return {k: self.coverage.get(k, 0) for k in keys}

    def under_covered(self) -> list[str]:
        return [k for k, v in self.coverage_report().items() if v < self.min_coverage]
