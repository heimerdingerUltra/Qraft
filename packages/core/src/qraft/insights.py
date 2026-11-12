"""Insights : analyse de causes d'une variation, détection d'anomalies, moniteurs.

Tout chiffre vient d'une intention QIR compilée puis exécutée par le moteur, avec les
mêmes politiques et la même identité qu'une question simple : jamais de SQL écrit à la
main. La narration est déterministe : elle ne cite que des nombres calculés.
"""

from __future__ import annotations

import math
import operator
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from statistics import mean, median
from typing import TYPE_CHECKING, Any, Literal, Protocol, Sequence, TextIO

from .execute import Result, SourceError
from .explain import Explainer, describe_period, french_date
from .graph import CompileError
from .policy import PolicyViolation, UnsafeSQL
from .qir import Dimension, Filter, Intent, Measure, TimeScope

if TYPE_CHECKING:
    from .compiler import Compiled
    from .engine import Engine


class InsightError(ValueError):
    """L'analyse demandée ne peut pas être menée avec ces données."""


class QueryBudgetExceeded(InsightError):
    """L'analyse demanderait plus de requêtes que le budget accordé."""


# --- Mise en forme française ----------------------------------------------------------------

_UNITS = {"EUR": "€", "USD": "$"}


def _dec(value: Any) -> Decimal:
    """Valeur numérique de la base -> Decimal exact (None compte pour zéro)."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def format_number(value: Any, *, unit: str | None = None, signed: bool = False, decimals: int | None = None) -> str:
    """Nombre au format français : « 1 234 567,89 € », « +12 », « -3,5 % »."""
    if value is None:
        return "n.d."
    number = _dec(value)
    suffix = f" {_UNITS.get(unit, unit)}" if unit else ""
    if not number.is_finite():
        return ("-" if number < 0 else "+" if signed else "") + "∞" + suffix
    if decimals is None:
        decimals = 2 if unit in ("EUR", "USD") or number != number.to_integral_value() else 0
    rounded = round(number, decimals)
    text = f"{abs(rounded):,.{decimals}f}".replace(",", " ").replace(".", ",")
    sign = "-" if rounded < 0 else ("+" if signed and rounded > 0 else "")
    return f"{sign}{text}{suffix}"


def format_pct(value: float | None, *, signed: bool = True) -> str:
    return "n.d." if value is None else format_number(value, unit="%", signed=signed, decimals=1)


def _value_label(value: Any) -> str:
    if value is None:
        return "(non renseigné)"
    if isinstance(value, datetime):
        return french_date(value.date().isoformat())
    if isinstance(value, date):
        return french_date(value.isoformat())
    return str(value)


def _period_label(scope: TimeScope) -> str:
    return scope.label or describe_period(scope.start, scope.end) or "sur toute la période"


# --- Analyse de causes ----------------------------------------------------------------------


@dataclass
class Segment:
    """Un segment d'une dimension et sa part de l'écart total."""

    dimension: str
    value: Any
    before: Decimal  # valeur du segment sur la période A (pour un ratio : le ratio du segment)
    after: Decimal
    contribution: Decimal  # part de l'écart total portée par ce segment
    share: float | None  # contribution / écart total
    mix_effect: Decimal | None = None  # ratios : effet de la répartition entre segments
    rate_effect: Decimal | None = None  # ratios : effet du taux propre au segment

    @property
    def label(self) -> str:
        return _value_label(self.value)

    def filter(self) -> Filter:
        if self.value is None:
            return Filter(column=self.dimension, op="is_null")
        return Filter(column=self.dimension, op="=", value=self.value)

    def to_dict(self) -> dict[str, Any]:
        def num(v: Decimal | None) -> float | None:
            return None if v is None else float(v)

        return {"dimension": self.dimension, "segment": self.label, "avant": num(self.before),
                "apres": num(self.after), "contribution": num(self.contribution),
                "part_de_l_ecart": None if self.share is None else round(self.share, 4),
                "effet_mix": num(self.mix_effect), "effet_taux": num(self.rate_effect)}


@dataclass
class Breakdown:
    """Décomposition de l'écart selon une dimension : segments classés, moteurs en tête."""

    dimension: str
    label: str
    segments: list[Segment]  # tous les segments, du plus moteur au plus opposé à la variation
    residual: Decimal  # écart total - somme des contributions (non nul si les segments se chevauchent)
    score: float  # poids du premier contributeur dans le sens de la variation
    top: int = 3
    direction: int = 0  # signe de l'écart total

    @property
    def drivers(self) -> list[Segment]:
        """Les `top` premiers contributeurs dans le sens de la variation."""
        if not self.direction:
            return self.segments[:self.top]
        return [s for s in self.segments if _sign(s.contribution) == self.direction][:self.top]

    @property
    def offsets(self) -> list[Segment]:
        """Segments à contre-courant, le plus opposé en premier."""
        if not self.direction:
            return []
        return [s for s in reversed(self.segments) if _sign(s.contribution) == -self.direction]

    @property
    def total_contribution(self) -> Decimal:
        return sum((s.contribution for s in self.segments), Decimal(0))

    def to_dict(self) -> dict[str, Any]:
        return {"dimension": self.dimension, "libelle": self.label, "residu": float(self.residual),
                "segments": len(self.segments), "contributeurs": [s.to_dict() for s in self.drivers],
                "contre_courant": [s.to_dict() for s in self.offsets[:1]]}


@dataclass
class ChangeExplanation:
    """Variation d'une métrique entre deux périodes, décomposée et racontée."""

    metric: str
    label: str
    unit: str | None
    kind: Literal["additif", "ratio"]
    period_a: TimeScope
    period_b: TimeScope
    filters: tuple[Filter, ...]
    before: Decimal
    after: Decimal
    delta: Decimal
    pct: float | None
    breakdowns: list[Breakdown] = field(default_factory=list)
    focus: Segment | None = None  # segment dans lequel l'analyse descend
    drilldown: ChangeExplanation | None = None
    queries: list[str] = field(default_factory=list)  # SQL exécuté à ce niveau
    tables: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    narrative: str = ""

    @property
    def query_count(self) -> int:
        return len(self.queries) + (self.drilldown.query_count if self.drilldown else 0)

    def all_tables(self) -> set[str]:
        return set(self.tables) | (self.drilldown.all_tables() if self.drilldown else set())

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrique": self.metric, "libelle": self.label, "unite": self.unit, "nature": self.kind,
            "periode_a": _period_label(self.period_a), "periode_b": _period_label(self.period_b),
            "filtres": [f"{f.column} {f.op} {f.value}" if f.value is not None else f"{f.column} {f.op}"
                        for f in self.filters],
            "avant": float(self.before), "apres": float(self.after), "ecart": float(self.delta),
            "evolution_pct": None if self.pct is None else round(self.pct, 2),
            "decompositions": [b.to_dict() for b in self.breakdowns],
            "segment_explore": self.focus.to_dict() if self.focus else None,
            "detail": self.drilldown.to_dict() if self.drilldown else None,
            "avertissements": self.warnings, "requetes": self.query_count, "narration": self.narrative,
        }


class _Runner:
    """Compile et exécute des intentions pour l'analyse, en comptant les requêtes."""

    def __init__(self, engine: Engine, max_queries: int | None) -> None:
        self.engine = engine
        self.max_queries = max_queries
        self.count = 0

    def execute(self, compiled: Compiled, node: ChangeExplanation) -> Result:
        if self.max_queries is not None and self.count >= self.max_queries:
            raise QueryBudgetExceeded(f"budget de requêtes atteint ({self.max_queries})")
        result = self.engine.source.query(compiled.sql, max_rows=self.engine.policy.max_rows)
        self.count += 1
        node.queries.append(compiled.sql)
        node.tables.update(compiled.tables)
        if result.truncated:
            node.warnings.append("résultat tronqué : la décomposition peut être incomplète")
        return result

    def run(self, intent: Intent, node: ChangeExplanation) -> Result:
        return self.execute(self.engine.compile(intent), node)


_RATIO = re.compile(r"^\s*\{(\w+)\}\s*/\s*\{(\w+)\}\s*$")


def _ratio_parts(engine: Engine, metric: str) -> tuple[str, str] | None:
    definition = engine.graph.metric(metric)
    match = _RATIO.match(definition.expression) if definition.is_composite else None
    return (match.group(1), match.group(2)) if match else None


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def explain_change(
    engine: Engine,
    metric: str,
    period_a: TimeScope,
    period_b: TimeScope,
    dimensions: Sequence[str] | None = None,
    depth: int = 2,
    top: int = 3,
    *,
    filters: Sequence[Filter] = (),
    max_queries: int | None = None,
) -> ChangeExplanation:
    """Explique la variation d'une métrique entre deux périodes (A puis B).

    1. Valeur de la métrique sur chaque période et écart.
    2. Décomposition selon chaque dimension candidate (par défaut, toutes celles du glossaire
       compatibles avec la métrique ; celles dont les segments se chevauchent sont écartées).
    3. Contribution de chaque segment : écart du segment pour une métrique additive ; effet
       mix + effet taux pour un ratio « {a} / {b} ». La somme des contributions et du résidu
       égale l'écart total.
    4. Classement des contributeurs, puis récursion dans le premier segment avec les
       dimensions restantes, jusqu'à depth niveaux.
    """
    if depth < 1:
        raise InsightError("la profondeur d'analyse doit valoir au moins 1")
    engine.graph.metric(metric)  # métrique inconnue : CompileError explicite
    candidates = list(dimensions) if dimensions is not None else list(engine.graph.semantic.dimensions)
    runner = _Runner(engine, max_queries)
    return _explain(engine, runner, metric, period_a, period_b, candidates, depth, max(1, top), tuple(filters))


def _explain(engine: Engine, runner: _Runner, metric: str, period_a: TimeScope, period_b: TimeScope,
             candidates: list[str], depth: int, top: int, filters: tuple[Filter, ...]) -> ChangeExplanation:
    definition = engine.graph.metric(metric)
    ratio = _ratio_parts(engine, metric)
    node = ChangeExplanation(
        metric=metric, label=engine.graph.label("metric", metric), unit=definition.unit,
        kind="ratio" if ratio else "additif", period_a=period_a, period_b=period_b, filters=filters,
        before=Decimal(0), after=Decimal(0), delta=Decimal(0), pct=None)
    measures = (Measure(metric=ratio[0]), Measure(metric=ratio[1])) if ratio else (Measure(metric=metric),)

    def totals(scope: TimeScope) -> tuple[Decimal, ...]:
        result = runner.run(Intent(measures=measures, filters=filters, time=scope), node)
        row = result.rows[0] if result.rows else (None,) * len(measures)
        return tuple(_dec(v) for v in row[-len(measures):])

    total_a, total_b = totals(period_a), totals(period_b)
    if ratio:
        if total_a[1] == 0 or total_b[1] == 0:
            raise InsightError(f"{node.label} : dénominateur nul sur une des périodes, ratio non défini")
        node.before, node.after = total_a[0] / total_a[1], total_b[0] / total_b[1]
    else:
        node.before, node.after = total_a[0], total_b[0]
    node.delta = node.after - node.before
    node.pct = float(100 * node.delta / abs(node.before)) if node.before != 0 else None

    direction = _sign(node.delta)
    found: list[tuple[int, Breakdown]] = []
    for position, dim in enumerate(candidates):
        breakdown = _breakdown(engine, runner, node, dim, measures, ratio is not None, top, direction)
        if breakdown is not None:
            found.append((position, breakdown))
    node.breakdowns = [b for _, b in sorted(found, key=lambda item: (-item[1].score, item[0]))]

    if node.breakdowns and node.breakdowns[0].segments and node.breakdowns[0].score > 0:
        node.focus = node.breakdowns[0].segments[0]
        remaining = [d for d in candidates if d != node.focus.dimension]
        if depth > 1 and remaining:
            node.drilldown = _explain(engine, runner, metric, period_a, period_b, remaining, depth - 1, top,
                                      filters + (node.focus.filter(),))
    node.narrative = _narrate(engine, node)
    return node


def _breakdown(engine: Engine, runner: _Runner, node: ChangeExplanation, dim: str, measures: tuple[Measure, ...],
               is_ratio: bool, top: int, direction: int) -> Breakdown | None:
    label = engine.graph.label("dimension", dim)
    per_period: list[dict[Any, tuple[Decimal, ...]]] = []
    for scope in (node.period_a, node.period_b):
        intent = Intent(measures=measures, dimensions=(Dimension(column=dim),), filters=node.filters, time=scope)
        try:
            compiled = engine.compile(intent)
        except CompileError as error:
            node.warnings.append(f"dimension écartée : {label} ({error})")
            return None
        if compiled.signals.get("fanout"):
            node.warnings.append(f"dimension écartée : {label} (ses segments se chevauchent pour cette métrique)")
            return None
        result = runner.execute(compiled, node)
        per_period.append({row[0]: tuple(_dec(v) for v in row[1:1 + len(measures)]) for row in result.rows})
    seg_a, seg_b = per_period
    keys = list(dict.fromkeys([*seg_a, *seg_b]))
    if len(keys) < 2:
        return None  # un seul segment : la dimension n'explique rien
    zero = (Decimal(0),) * len(measures)
    segments: list[Segment] = []
    if is_ratio:
        num_a = sum((seg_a.get(k, zero)[0] for k in keys), Decimal(0))
        den_a = sum((seg_a.get(k, zero)[1] for k in keys), Decimal(0))
        den_b = sum((seg_b.get(k, zero)[1] for k in keys), Decimal(0))
        ratio_a = num_a / den_a if den_a else Decimal(0)
        for k in keys:
            (na, da), (nb, db) = seg_a.get(k, zero), seg_b.get(k, zero)
            weight_a = da / den_a if den_a else Decimal(0)
            weight_b = db / den_b if den_b else Decimal(0)
            rate_a = na / da if da else ratio_a  # segment absent en A : aucun effet mix
            rate_b = nb / db if db else Decimal(0)
            mix = (weight_b - weight_a) * (rate_a - ratio_a)
            rate = weight_b * (rate_b - rate_a) if db else Decimal(0)
            segments.append(Segment(dim, k, na / da if da else Decimal(0), rate_b, mix + rate, None, mix, rate))
    else:
        for k in keys:
            a, b = seg_a.get(k, zero)[0], seg_b.get(k, zero)[0]
            segments.append(Segment(dim, k, a, b, b - a, None))
    for s in segments:
        s.share = float(s.contribution / node.delta) if node.delta != 0 else None

    def drive(s: Segment) -> Decimal:
        return s.contribution * direction if direction else abs(s.contribution)

    segments.sort(key=lambda s: (-drive(s), s.label))
    residual = node.delta - sum((s.contribution for s in segments), Decimal(0))
    return Breakdown(dim, label, segments, residual, float(drive(segments[0])), top, direction)


# --- Narration déterministe -----------------------------------------------------------------


def _narrate(engine: Engine, node: ChangeExplanation, prefix: str = "") -> str:
    explainer = Explainer(engine.graph)
    unit = node.unit

    def amount(v: Decimal, signed: bool = False) -> str:
        return format_number(v, unit=unit, signed=signed)

    head = node.label[:1].upper() + node.label[1:]
    if node.filters:
        head += " (" + ", ".join(explainer.filter_text(f) for f in node.filters) + ")"
    pct = f", {format_pct(node.pct)}" if node.pct is not None else ""
    lines = [f"{prefix}{head} : {amount(node.before)} {_period_label(node.period_a)}, "
             f"{amount(node.after)} {_period_label(node.period_b)}, soit {amount(node.delta, True)}{pct}."]
    if not node.breakdowns:
        lines.append("Aucune dimension exploitable pour décomposer l'écart.")
    else:
        best = node.breakdowns[0]

        def describe(s: Segment) -> str:
            share = f", {format_pct(100 * s.share, signed=False)} de l'écart" if s.share is not None else ""
            effects = ""
            if s.mix_effect is not None and s.rate_effect is not None:
                effects = f" : mix {amount(s.mix_effect, True)}, taux {amount(s.rate_effect, True)}"
            return f"{s.label} ({amount(s.contribution, True)}{share}{effects})"

        lines.append(f"Par {best.label}, l'écart vient surtout de : "
                     + ", ".join(describe(s) for s in best.drivers) + ".")
        if best.offsets:
            against = best.offsets[0]
            lines.append(f"À contre-courant : {against.label} ({amount(against.contribution, True)}).")
        if node.kind == "ratio":
            mix = sum((s.mix_effect or Decimal(0) for s in best.segments), Decimal(0))
            rate = sum((s.rate_effect or Decimal(0) for s in best.segments), Decimal(0))
            lines.append(f"Effet mix (répartition entre segments) : {amount(mix, True)} ; "
                         f"effet taux (évolution dans chaque segment) : {amount(rate, True)}.")
        if abs(best.residual) > Decimal("1e-9") * max(Decimal(1), abs(node.delta)):
            lines.append(f"Écart non réparti entre les segments : {amount(best.residual, True)}.")
        others = [f"{b.label} ({b.segments[0].label}, {amount(b.segments[0].contribution, True)})"
                  for b in node.breakdowns[1:]]
        if others:
            lines.append("Autres axes, premier contributeur : " + " ; ".join(others) + ".")
    if node.drilldown is not None and node.focus is not None:
        lines.append(_narrate(engine, node.drilldown, prefix=f"Dans « {node.focus.label} » — "))
    return "\n".join(lines)


# --- Anomalies ------------------------------------------------------------------------------


@dataclass
class Anomaly:
    when: Any
    value: float
    expected: float  # médiane de la fenêtre de référence
    score: float  # z-score modifié (Iglewicz et Hoaglin)
    direction: Literal["hausse", "baisse"]

    def to_dict(self) -> dict[str, Any]:
        when = self.when.isoformat() if hasattr(self.when, "isoformat") else self.when
        return {"date": when, "valeur": self.value, "attendu": self.expected, "score": self.score,
                "sens": self.direction}


_MAD_SCALE = 0.6745  # rend la MAD comparable à un écart-type sous hypothèse normale
_MEAN_AD_SCALE = 1.253314  # repli quand la MAD est nulle (plus de la moitié des valeurs identiques)


def detect_anomalies(points: Sequence[tuple[Any, Any]], *, threshold: float = 3.5, window: int = 12) -> list[Anomaly]:
    """Points anormaux d'une série [(date, valeur)], par médiane et MAD glissantes.

    La référence d'un point est la fenêtre des `window` points qui le précèdent (le point
    lui-même exclu, pour qu'une valeur aberrante ne masque pas sa propre détection). Les
    premiers points, sans historique complet, sont comparés aux `window` autres points de
    la fenêtre initiale. Score : z-score modifié 0,6745 × (x − médiane) / MAD ; le point est
    anormal quand |score| dépasse threshold. Pas de prise en compte de la saisonnalité.
    """
    if window < 3:
        raise InsightError("la fenêtre de détection doit couvrir au moins 3 points")
    clean = [(when, float(value)) for when, value in points if value is not None]
    values = [v for _, v in clean]
    if len(values) < 4:
        return []
    found: list[Anomaly] = []
    for i, (when, value) in enumerate(clean):
        reference = values[max(0, i - window):i]
        if len(reference) < window:
            reference = [values[j] for j in range(min(len(values), window + 1)) if j != i]
        center = median(reference)
        deviations = [abs(x - center) for x in reference]
        mad = median(deviations)
        if mad > 0:
            score = _MAD_SCALE * (value - center) / mad
        elif (mean_ad := mean(deviations)) > 0:
            score = (value - center) / (_MEAN_AD_SCALE * mean_ad)
        else:
            score = 0.0 if value == center else math.copysign(math.inf, value - center)
        if abs(score) > threshold:
            found.append(Anomaly(when, value, center, round(score, 2), "hausse" if score > 0 else "baisse"))
    return found


# --- Moniteurs ------------------------------------------------------------------------------

_OPERATORS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
              "=": operator.eq, "!=": operator.ne}
_OP_TEXT = {">": "au-dessus du seuil de", ">=": "au niveau ou au-dessus du seuil de", "<": "sous le seuil de",
            "<=": "au niveau ou sous le seuil de", "=": "égal à", "!=": "différent de"}


@dataclass(frozen=True)
class ThresholdCondition:
    """Alerte quand la mesure franchit un seuil (op appliqué à la valeur : valeur op seuil)."""

    op: Literal[">", ">=", "<", "<=", "=", "!="]
    value: float

    def __post_init__(self) -> None:
        if self.op not in _OPERATORS:
            raise ValueError(f"opérateur de seuil inconnu : {self.op}")


@dataclass(frozen=True)
class AnomalyCondition:
    """Alerte quand l'un des `recent` derniers points d'une série est anormal."""

    threshold: float = 3.5
    window: int = 12
    recent: int = 1


@dataclass
class Monitor:
    """Une question (ou une intention) surveillée, avec sa condition d'alerte.

    Le moniteur n'a pas plus de droits qu'une question : il passe par le même moteur.
    `column` choisit la mesure surveillée (par défaut, la première après les dimensions).
    Une condition de seuil s'applique à chaque ligne du résultat ; une condition d'anomalie
    demande une série (une seule dimension, ordonnée, typiquement temporelle).
    """

    name: str
    condition: ThresholdCondition | AnomalyCondition
    question: str | None = None
    intent: Intent | None = None
    column: str | None = None

    def __post_init__(self) -> None:
        if (self.question is None) == (self.intent is None):
            raise ValueError("un moniteur porte soit une question, soit une intention (exactement une des deux)")


@dataclass
class Alert:
    monitor: str
    kind: Literal["seuil", "anomalie", "erreur"]
    message: str
    value: Any = None
    breaches: list[tuple[str, Any]] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    delivery_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def clean(v: Any) -> Any:
            if isinstance(v, Decimal):
                return float(v)
            return v.isoformat() if hasattr(v, "isoformat") else v

        return {"moniteur": self.monitor, "type": self.kind, "message": self.message, "valeur": clean(self.value),
                "depassements": [[label, clean(v)] for label, v in self.breaches],
                "anomalies": [a.to_dict() for a in self.anomalies], "date": self.at}


class MonitorError(RuntimeError):
    """Le moniteur ne peut pas être évalué."""


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


class NotificationError(RuntimeError):
    """L'alerte n'a pas pu être remise."""


class ConsoleNotifier:
    """Écrit les alertes sur la console (ou tout flux texte)."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream

    def send(self, alert: Alert) -> None:
        print(f"[{alert.kind.upper()}] {alert.message}", file=self.stream or sys.stdout)


class WebhookNotifier:
    """Poste l'alerte en JSON vers un webhook (Slack, Teams, outil maison).

    Le corps contient « text » (le message, compris par les webhooks entrants usuels) et
    « alert » (le détail structuré). L'URL est injectée et n'apparaît jamais dans le corps.
    """

    def __init__(self, url: str, *, client: Any | None = None, timeout_s: float = 10.0,
                 headers: dict[str, str] | None = None) -> None:
        if client is None:
            import httpx

            client = httpx.Client(timeout=timeout_s)
        self.url = url
        self.client = client
        self.headers = dict(headers or {})

    def send(self, alert: Alert) -> None:
        payload = {"text": alert.message, "alert": alert.to_dict()}
        try:
            response = self.client.post(self.url, json=payload, headers=self.headers)
            response.raise_for_status()
        except Exception as error:  # noqa: BLE001 - toute erreur de transport devient une erreur lisible
            raise NotificationError(f"échec de l'envoi au webhook : {error}") from error


def _evaluate(engine: Engine, monitor: Monitor) -> tuple[list[str], list[tuple[Any, ...]], int]:
    if monitor.intent is not None:
        compiled = engine.compile(monitor.intent)
        result = engine.source.query(compiled.sql, max_rows=engine.policy.max_rows)
        return compiled.columns or result.columns, result.rows, int(compiled.signals.get("dims", 0))
    answer = engine.ask(monitor.question or "")
    if answer.status not in ("ok", "a_confirmer") or answer.intent is None:
        raise MonitorError(answer.message or f"question non résolue (statut {answer.status})")
    return answer.columns, answer.rows, len(answer.intent.dimensions)


def _check(engine: Engine, monitor: Monitor) -> Alert | None:
    columns, rows, n_dims = _evaluate(engine, monitor)
    if monitor.column is not None and monitor.column not in columns:
        raise MonitorError(f"colonne surveillée absente du résultat : {monitor.column}")
    if len(columns) <= n_dims:
        raise MonitorError("le résultat ne contient aucune mesure")
    column = monitor.column or columns[n_dims]
    index = columns.index(column)
    metric = engine.graph.semantic.metrics.get(column)
    label = metric.label or metric.id if metric else column.replace("_", " ")
    unit = metric.unit if metric else None
    name = f"Moniteur « {monitor.name} »"
    condition = monitor.condition

    if isinstance(condition, ThresholdCondition):
        test = _OPERATORS[condition.op]
        threshold = format_number(condition.value, unit=unit)
        if n_dims == 0:
            value = rows[0][index] if rows else None
            if value is None or not test(float(value), condition.value):
                return None
            return Alert(monitor.name, "seuil", f"{name} : {label} vaut {format_number(value, unit=unit)}, "
                         f"{_OP_TEXT[condition.op]} {threshold}.", value=value)
        breaches = [(" / ".join(_value_label(v) for v in row[:n_dims]), row[index]) for row in rows
                    if row[index] is not None and test(float(row[index]), condition.value)]
        if not breaches:
            return None
        listed = ", ".join(f"{segment} ({format_number(v, unit=unit)})" for segment, v in breaches[:10])
        more = f" et {len(breaches) - 10} autre(s)" if len(breaches) > 10 else ""
        return Alert(monitor.name, "seuil", f"{name} : {len(breaches)} valeur(s) de {label} "
                     f"{_OP_TEXT[condition.op]} {threshold} : {listed}{more}.", breaches=breaches)

    if n_dims != 1:
        raise MonitorError("la détection d'anomalies demande une série : une seule dimension, temporelle")
    points = [(row[0], row[index]) for row in rows]
    try:
        points.sort(key=lambda p: (p[0] is None, p[0]))
    except TypeError:
        pass  # valeurs non comparables : on garde l'ordre de la requête
    recent = {p[0] for p in points[-max(1, condition.recent):]}
    anomalies = [a for a in detect_anomalies(points, threshold=condition.threshold, window=condition.window)
                 if a.when in recent]
    if not anomalies:
        return None
    details = " ; ".join(
        f"{_value_label(a.when)} : {format_number(a.value, unit=unit)} contre {format_number(a.expected, unit=unit)}"
        f" attendu ({a.direction}, score {format_number(a.score, signed=True, decimals=1)})" for a in anomalies)
    return Alert(monitor.name, "anomalie", f"{name} : valeur anormale de {label} — {details}.",
                 value=anomalies[-1].value, anomalies=anomalies)


def run_monitors(engine: Engine, monitors: Sequence[Monitor], *, notifiers: Sequence[Notifier] = ()) -> list[Alert]:
    """Évalue les moniteurs et renvoie les alertes (déclenchements et moniteurs en échec).

    Chaque alerte est remise à chaque notificateur ; un échec de remise est consigné dans
    l'alerte (delivery_errors) sans interrompre les autres envois.
    """
    alerts: list[Alert] = []
    for monitor in monitors:
        try:
            alert = _check(engine, monitor)
        except (MonitorError, CompileError, PolicyViolation, UnsafeSQL, SourceError, InsightError) as error:
            alert = Alert(monitor.name, "erreur", f"Moniteur « {monitor.name} » : évaluation impossible : {error}")
        if alert is None:
            continue
        for notifier in notifiers:
            try:
                notifier.send(alert)
            except Exception as error:  # noqa: BLE001 - un canal en panne ne bloque pas les autres
                alert.delivery_errors.append(f"{type(notifier).__name__} : {error}")
        alerts.append(alert)
    return alerts
