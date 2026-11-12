"""Compilateur sémantique : QIR -> SQL dialectal.

Passes : résolution des références, expansion des métriques (composées, filtres
intrinsèques, règles globales), regroupement des mesures par grain, planification
des jointures, traitement des pièges de grain (semi-jointures, CTE pré-agrégées),
intelligence temporelle, politiques, abaissement dialectal via SQLGlot.

Les jointures ne viennent jamais du modèle : elles sont calculées sur le graphe.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import sqlglot
from sqlglot import exp

from .catalog import key_column
from .graph import CompileError, JoinPlan, KnowledgeGraph
from .policy import Identity, Policy, PolicyViolation, check_columns, check_read_only, check_tree, row_filter
from .qir import GRAIN_ALIASES, Filter, Intent, Measure, TimeScope
from .text import strip_accents

NO_FULL_OUTER_JOIN = {"mysql"}
# Clé des CTE dans l'AST SQLGlot : « with_ » depuis les versions récentes, « with » avant.
WITH_KEY = "with_" if "with_" in exp.Select.arg_types else "with"
LIMIT_KEY = "limit"


@dataclass
class Atom:
    """Une mesure élémentaire : un agrégat sur une table de base, avec ses filtres."""

    alias: str
    expr: exp.Expression
    base: str
    filters: list[exp.Expression]
    time_column: str | None
    hidden: bool = False

    def group_key(self) -> tuple[str, str | None, tuple[str, ...]]:
        return self.base, self.time_column, tuple(sorted(f.sql() for f in self.filters))


@dataclass
class Output:
    """Une colonne de sortie : un atome, ou une formule sur des atomes."""

    alias: str
    formula: exp.Expression
    direct: bool = True


@dataclass
class DimSpec:
    alias: str
    ref: str | None  # « table.colonne », ou None pour la colonne temporelle de la mesure
    grain: str | None


@dataclass
class Compiled:
    sql: str
    dialect: str
    ast: exp.Expression
    columns: list[str]
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(self.sql.encode()).hexdigest()[:16]


def _col(ref: str) -> exp.Column:
    table, column = ref.rsplit(".", 1)
    return exp.column(column, table=table)


def _tables_of(node: exp.Expression) -> set[str]:
    return {c.table for c in node.find_all(exp.Column) if c.table}


def _date(value: str) -> exp.Expression:
    return exp.cast(exp.Literal.string(value), "DATE")


def _literal(value: Any, temporal: bool) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    text = str(value)
    if temporal and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return _date(text)
    return exp.Literal.string(text)


def _safe_division(node: exp.Expression) -> exp.Expression:
    def wrap(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Div) and not isinstance(n.expression, exp.Nullif):
            return exp.Div(this=n.this, expression=exp.Nullif(this=n.expression, expression=exp.Literal.number(0)))
        return n

    return node.transform(wrap)


def shift_scope(scope: TimeScope) -> TimeScope:
    """Période de comparaison : un an plus tôt, ou la période de même durée qui précède."""
    if not scope.start or not scope.end:
        raise CompileError("une comparaison temporelle demande une période bornée")
    start, end = date.fromisoformat(scope.start), date.fromisoformat(scope.end)
    if scope.compare == "previous_year":
        def back(d: date) -> date:
            try:
                return d.replace(year=d.year - 1)
            except ValueError:  # 29 février
                return d.replace(year=d.year - 1, day=28)
        new_start, new_end = back(start), back(end)
    else:
        length = end - start
        new_start, new_end = start - length, start
    return TimeScope(start=new_start.isoformat(), end=new_end.isoformat(), column=scope.column)


class Compiler:
    def __init__(self, graph: KnowledgeGraph, *, policy: Policy | None = None, identity: Identity | None = None,
                 known_at: str | None = None) -> None:
        self.graph = graph
        self.policy = policy or Policy()
        self.identity = identity or Identity()
        self.read_dialect = graph.dialect
        self.write_dialect = graph.catalog.dialect
        self.as_of: str | None = None  # date métier qui choisit la version des métriques (début de période)
        self.known_at: str | None = known_at  # temps de connaissance (reproductibilité)
        self._report: Compiled | None = None

    # --- Résolution -------------------------------------------------------------------------

    def _parse(self, sql: str) -> exp.Expression:
        return sqlglot.parse_one(sql, read=self.read_dialect)

    def _resolve_ref(self, ref: str) -> str:
        if ref in self.graph.semantic.dimensions:
            return self.graph.dimension(ref)[0]
        tbl, col = self.graph.column(ref)
        return f"{tbl.name}.{col.name}"

    def filter_expr(self, f: Filter) -> exp.Expression:
        ref = self._resolve_ref(f.column)
        _, column = self.graph.column(ref)
        c = _col(ref)
        values = f.value if isinstance(f.value, (list, tuple)) else [f.value]
        lit = [_literal(v, column.is_temporal) for v in values]
        builders = {
            "=": lambda: exp.EQ(this=c, expression=lit[0]),
            "!=": lambda: exp.NEQ(this=c, expression=lit[0]),
            "<": lambda: exp.LT(this=c, expression=lit[0]),
            "<=": lambda: exp.LTE(this=c, expression=lit[0]),
            ">": lambda: exp.GT(this=c, expression=lit[0]),
            ">=": lambda: exp.GTE(this=c, expression=lit[0]),
            "in": lambda: exp.In(this=c, expressions=lit),
            "not_in": lambda: exp.Not(this=exp.In(this=c, expressions=lit)),
            "between": lambda: exp.Between(this=c, low=lit[0], high=lit[-1]),
            "contains": lambda: exp.Like(this=exp.Lower(this=c),
                                         expression=exp.Literal.string(f"%{str(values[0]).lower()}%")),
            "is_null": lambda: exp.Is(this=c, expression=exp.Null()),
            "not_null": lambda: exp.Not(this=exp.Is(this=c, expression=exp.Null())),
        }
        return builders[f.op]()

    def _atomic_metric(self, metric_id: str, alias: str, extra: list[exp.Expression], hidden: bool) -> Atom:
        metric, version = self.graph.metric(metric_id).at(self.as_of, known_at=self.known_at)
        if version is not None and self._report is not None:
            note = f"définition de « {metric.label or metric.id} » en vigueur depuis le {version.valid_from}"
            if note not in self._report.rules:
                self._report.rules.append(note + (f" ({version.note})" if version.note else ""))
        expr = self._parse(metric.expression)
        tables = _tables_of(expr)
        base = metric.table or (next(iter(tables)) if len(tables) == 1 else None)
        if base is None:
            raise CompileError(f"la métrique {metric_id} doit déclarer sa table de grain (champ table)")
        base = self.graph.table(base).name
        filters = [self._parse(f) for f in metric.filters] + list(extra)
        time_column = metric.time_column or self.graph.time_column(base)
        if time_column:
            time_column = self._resolve_ref(time_column)
        return Atom(alias, expr, base, filters, time_column, hidden)

    def measure_atoms(self, m: Measure, alias_hint: str | None = None, hidden: bool = False) -> tuple[list[Atom], Output]:
        extra = [self.filter_expr(f) for f in m.filters]
        if m.metric:
            metric = self.graph.metric(m.metric)
            alias = m.alias or alias_hint or metric.id
            if metric.is_composite:
                formula_sql = metric.expression
                atoms = []
                for name in dict.fromkeys(re.findall(r"\{(\w+)\}", metric.expression)):
                    if self.graph.metric(name).is_composite:
                        raise CompileError(f"métrique composée imbriquée non prise en charge : {name}")
                    atom_alias = f"{alias}__{name}"
                    atoms.append(self._atomic_metric(name, atom_alias, extra, hidden=True))
                    formula_sql = formula_sql.replace("{" + name + "}", atom_alias)
                return atoms, Output(alias, _safe_division(self._parse(formula_sql)), direct=False)
            atom = self._atomic_metric(metric.id, alias, extra, hidden)
            return [atom], Output(alias, exp.column(alias))
        if not m.agg or not m.column:
            raise CompileError("une mesure doit citer une métrique, ou un agrégat et une colonne")
        if "." not in m.column and self.graph.catalog.table(m.column):
            base = self.graph.table(m.column).name
            expr: exp.Expression = exp.Count(this=exp.Star())
            alias = m.alias or alias_hint or f"nb_{base}"
        else:
            ref = self._resolve_ref(m.column)
            base, column = ref.rsplit(".", 1)
            c = _col(ref)
            aggregates = {
                "sum": lambda: exp.Sum(this=c), "avg": lambda: exp.Avg(this=c),
                "min": lambda: exp.Min(this=c), "max": lambda: exp.Max(this=c),
                "count": lambda: exp.Count(this=c),
                "count_distinct": lambda: exp.Count(this=exp.Distinct(expressions=[c])),
            }
            expr = aggregates[m.agg]()
            alias = m.alias or alias_hint or f"{m.agg}_{column}"
        time_column = self.graph.time_column(base)
        return [Atom(alias, expr, base, extra, time_column, hidden)], Output(alias, exp.column(alias))

    @staticmethod
    def _ratio_alias(r: Any) -> str:
        name = r.numerator.metric or (r.numerator.column or "mesure").split(".")[-1]
        parts = [name] + [str(f.value) for f in r.numerator.filters
                          if f.op == "=" and isinstance(f.value, (str, int))]
        alias = re.sub(r"[^a-z0-9_]+", "_", strip_accents("part_" + "_".join(parts)).lower()).strip("_")
        return alias + ("_pct" if r.percent else "")

    # --- Construction d'un groupe (un grain, une table de base) -----------------------------

    def _dim_expr(self, dim: DimSpec, time_column: str | None) -> exp.Expression:
        ref = dim.ref or time_column
        if ref is None:
            raise CompileError("aucune colonne temporelle connue pour ce regroupement par période")
        if not dim.grain:
            return _col(ref)
        column_sql = _col(ref).sql(dialect="postgres")
        return sqlglot.parse_one(f"CAST(DATE_TRUNC('{dim.grain}', {column_sql}) AS DATE)", read="postgres")

    def _build_group(
        self,
        atoms: list[Atom],
        base: str,
        time_column: str | None,
        dims: list[DimSpec],
        filters: list[exp.Expression],
        scope: TimeScope | None,
        report: Compiled,
    ) -> exp.Select:
        where: list[exp.Expression] = list(atoms[0].filters if atoms else []) + list(filters)
        if scope and (scope.start or scope.end):
            tc = scope.column and self._resolve_ref(scope.column) or time_column
            if tc is None:
                report.warnings.append(f"aucune colonne temporelle pour {base} : la période est ignorée")
            else:
                used = report.signals.setdefault("time_columns", [])
                if tc not in used:
                    used.append(tc)
                if scope.start:
                    where.append(exp.GTE(this=_col(tc), expression=_date(scope.start)))
                if scope.end:
                    where.append(exp.LT(this=_col(tc), expression=_date(scope.end)))
        dim_exprs = [(d.alias, self._dim_expr(d, time_column)) for d in dims]
        dim_tables = set().union(*(_tables_of(e) for _, e in dim_exprs)) if dim_exprs else set()
        measure_tables = set().union(*(_tables_of(a.expr) for a in atoms)) if atoms else set()
        filter_tables = set().union(*(_tables_of(w) for w in where)) if where else set()
        plan = self.graph.plan_joins(base, dim_tables | measure_tables | filter_tables)

        # Règles globales et filtres de lignes des tables traversées.
        for table_name in plan.tables:
            for rule in self.graph.semantic.rules:
                if rule.table == table_name:
                    where.append(self._parse(rule.filter))
                    label = rule.label or rule.filter
                    if label not in report.rules:
                        report.rules.append(label)
            if table_name in self.policy.row_filters:
                where.append(row_filter(self.policy.row_filters[table_name], self.identity, self.read_dialect))

        # Pièges de grain : une descente un-vers-plusieurs ne sert qu'à filtrer -> semi-jointure (EXISTS).
        semijoins = []
        fanout = False
        for step in plan.steps:
            if not step.fanout:
                continue
            subtree = plan.subtree(step.target)
            if subtree & (dim_tables | measure_tables):
                fanout = True
            elif not any(step.target in plan.subtree(s.target) for s in semijoins):
                semijoins.append(step)
        excluded = set().union(*(plan.subtree(s.target) for s in semijoins)) if semijoins else set()

        selected = [exp.alias_(e, a) for a, e in dim_exprs]
        for atom in atoms:
            expr = atom.expr
            if fanout and isinstance(expr, exp.Count) and isinstance(expr.this, exp.Star):
                key = key_column(self.graph.table(base))
                if key is not None:
                    expr = exp.Count(this=exp.Distinct(expressions=[_col(f"{base}.{key.name}")]))
            selected.append(exp.alias_(expr, atom.alias))
        query = exp.select(*selected).from_(exp.to_table(base))
        for step in plan.steps:
            if step.target in excluded:
                continue
            on = exp.EQ(this=_col(f"{step.edge.child}.{step.edge.child_col}"),
                        expression=_col(f"{step.edge.parent}.{step.edge.parent_col}"))
            query = query.join(exp.to_table(step.target), on=on, join_type="inner" if step.fanout else "left")

        main_tables = set(plan.tables) - excluded
        for step in semijoins:
            subtree = plan.subtree(step.target)
            inner_where = [w for w in where if _tables_of(w) and _tables_of(w) <= subtree]
            inner = exp.select(exp.Literal.number(1)).from_(exp.to_table(step.target))
            for s in plan.steps:
                if s.source in subtree and s.target in subtree:
                    on = exp.EQ(this=_col(f"{s.edge.child}.{s.edge.child_col}"),
                                expression=_col(f"{s.edge.parent}.{s.edge.parent_col}"))
                    inner = inner.join(exp.to_table(s.target), on=on, join_type="inner")
            link = exp.EQ(this=_col(f"{step.edge.child}.{step.edge.child_col}"),
                          expression=_col(f"{step.edge.parent}.{step.edge.parent_col}"))
            query = query.where(exp.Exists(this=inner.where(link, *inner_where)))
        for w in where:
            tables = _tables_of(w)
            if tables & excluded:
                if not tables <= excluded:
                    raise CompileError("un filtre mélange des tables de grains différents")
                continue
            if tables - main_tables:
                raise CompileError(f"filtre sur une table non jointe : {', '.join(sorted(tables - main_tables))}")
            query = query.where(w)

        if fanout:
            report.signals["fanout"] = True
            report.assumptions.append(
                f"une dimension est plus fine que la mesure de {base} : les montants de {base} peuvent se répéter"
            )
        if plan.ambiguous:
            report.signals["ambiguous_join"] = True
            report.assumptions += [f"plusieurs chemins de jointure possibles vers {t}" for t in plan.ambiguous]
        report.signals["inferred_edges"] = report.signals.get("inferred_edges", 0) + plan.inferred_edges
        for step in plan.steps:
            if step.edge.origin == "inferred":
                report.assumptions.append(
                    f"relation inférée utilisée : {step.edge.child}.{step.edge.child_col} -> "
                    f"{step.edge.parent}.{step.edge.parent_col}")
        report.tables += [t for t in plan.tables if t not in report.tables]

        if atoms and dim_exprs:
            query = query.group_by(*[e for _, e in dim_exprs])
        elif not atoms:
            query = query.distinct()
        return query

    # --- Assemblage -------------------------------------------------------------------------

    def _measures_query(
        self, intent: Intent, scope: TimeScope | None, prefix: str, report: Compiled
    ) -> tuple[exp.Select, list[DimSpec], list[str]]:
        atoms: list[Atom] = []
        outputs: list[Output] = []
        for m in intent.measures:
            new_atoms, output = self.measure_atoms(m)
            atoms += new_atoms
            outputs.append(output)
        for r in intent.ratios:
            base_alias = r.alias or self._ratio_alias(r)

            def side(m: Measure, tag: str) -> tuple[list[Atom], exp.Expression]:
                side_atoms, out = self.measure_atoms(m, hidden=True)
                mapping: dict[str, str] = {}
                for i, a in enumerate(side_atoms):
                    mapping[a.alias] = f"{base_alias}__{tag}{i}"
                    a.alias, a.hidden = mapping[a.alias], True
                renamed = out.formula.transform(
                    lambda n: exp.column(mapping[n.name])
                    if isinstance(n, exp.Column) and not n.table and n.name in mapping else n)
                return side_atoms, renamed

            num_atoms, num_f = side(r.numerator, "num")
            den_atoms, den_f = side(r.denominator, "den")
            formula = exp.Div(this=num_f, expression=exp.Nullif(this=den_f, expression=exp.Literal.number(0)))
            if r.percent:
                formula = exp.Mul(this=exp.Literal.number(100.0), expression=exp.paren(formula))
            atoms += num_atoms + den_atoms
            outputs.append(Output(base_alias, formula, direct=False))

        dims = []
        for d in intent.dimensions:
            if d.column == "time":
                grain = d.grain or "day"
                dims.append(DimSpec(d.alias or GRAIN_ALIASES[grain], None, grain))
            else:
                ref, dim_def = self.graph.dimension(d.column)
                alias = d.alias or (dim_def.id if dim_def else ref.split(".")[1])
                if d.grain:
                    alias = d.alias or f"{alias}_{GRAIN_ALIASES[d.grain]}"
                dims.append(DimSpec(alias, ref, d.grain))
        filters = [self.filter_expr(f) for f in intent.filters]

        if not atoms:  # liste de valeurs (pas de mesure)
            if not dims:
                raise CompileError("l'intention ne contient ni mesure ni dimension")
            first = dims[0].ref.split(".")[0] if dims[0].ref else None
            if first is None:
                raise CompileError("une liste demande une dimension sur une colonne")
            query = self._build_group([], first, self.graph.time_column(first), dims, filters, scope, report)
            return query, dims, [d.alias for d in dims]

        groups: dict[tuple[str, str | None, tuple[str, ...]], list[Atom]] = {}
        for atom in atoms:
            groups.setdefault(atom.group_key(), []).append(atom)
        selects = [
            (f"{prefix}g{i}", group_atoms, self._build_group(group_atoms, key[0], key[1], dims, filters, scope, report))
            for i, (key, group_atoms) in enumerate(groups.items(), start=1)
        ]
        report.signals["groups"] = len(selects)

        if len(selects) == 1:
            core = selects[0][2]
        else:
            names = [name for name, _, _ in selects]
            columns: list[exp.Expression] = []
            for d in dims:
                parts = [exp.column(d.alias, table=n) for n in names]
                columns.append(exp.alias_(exp.Coalesce(this=parts[0], expressions=parts[1:]), d.alias))
            for name, group_atoms, _ in selects:
                columns += [exp.alias_(exp.column(a.alias, table=name), a.alias) for a in group_atoms]
            core = exp.select(*columns).from_(exp.to_table(names[0]))
            join_type = "left" if self.write_dialect in NO_FULL_OUTER_JOIN else "full"
            for i, name in enumerate(names[1:], start=1):
                if dims:
                    conds = []
                    for d in dims:
                        prev = [exp.column(d.alias, table=n) for n in names[:i]]
                        left = prev[0] if len(prev) == 1 else exp.Coalesce(this=prev[0], expressions=prev[1:])
                        conds.append(exp.EQ(this=left, expression=exp.column(d.alias, table=name)))
                    core = core.join(exp.to_table(name), on=exp.and_(*conds), join_type=join_type)
                else:
                    core = core.join(exp.to_table(name), join_type="cross")
            for name, _, select in selects:
                core = core.with_(name, as_=select)
            if join_type == "left":
                report.assumptions.append("jointure externe complète indisponible : groupes joints à gauche")

        if all(o.direct for o in outputs) and not any(a.hidden for a in atoms):
            return core, dims, [d.alias for d in dims] + [o.alias for o in outputs]

        wrapper_name = f"{prefix}mesures"
        hoisted = core.args.get(WITH_KEY)
        core.set(WITH_KEY, None)
        final = exp.select(*[exp.column(d.alias) for d in dims],
                           *[exp.alias_(o.formula, o.alias) for o in outputs]).from_(exp.to_table(wrapper_name))
        if hoisted is not None:
            final.set(WITH_KEY, hoisted)
        final = final.with_(wrapper_name, as_=core)
        return final, dims, [d.alias for d in dims] + [o.alias for o in outputs]

    def _compare(self, intent: Intent, report: Compiled) -> tuple[exp.Select, list[DimSpec], list[str]]:
        scope = intent.time
        assert scope is not None and scope.compare
        if any(d.column == "time" or d.grain for d in intent.dimensions):
            raise CompileError("comparaison temporelle avec un regroupement par période : non pris en charge en v0")
        previous_scope = shift_scope(scope)
        self.as_of = scope.start  # chaque période prend la définition en vigueur à son début
        current, dims, cols = self._measures_query(intent, scope, "c_", report)
        self.as_of = previous_scope.start
        previous, _, _ = self._measures_query(intent, previous_scope, "p_", report)
        measure_cols = cols[len(dims):]
        hoisted = []
        for q in (current, previous):
            w = q.args.get(WITH_KEY)
            if w is not None:
                hoisted += list(w.expressions)
                q.set(WITH_KEY, None)
        selected: list[exp.Expression] = []
        for d in dims:
            selected.append(exp.alias_(exp.Coalesce(this=exp.column(d.alias, table="courant"),
                                                    expressions=[exp.column(d.alias, table="precedent")]), d.alias))
        out_cols = [d.alias for d in dims]
        for m in measure_cols:
            cur, prev = exp.column(m, table="courant"), exp.column(m, table="precedent")
            delta = exp.Mul(this=exp.Literal.number(100.0), expression=exp.paren(exp.Div(
                this=exp.paren(exp.Sub(this=cur, expression=prev)),
                expression=exp.Nullif(this=prev.copy(), expression=exp.Literal.number(0)))))
            if scope.compare_output == "all":
                selected += [exp.alias_(cur.copy(), m), exp.alias_(prev.copy(), f"{m}_precedent")]
                out_cols += [m, f"{m}_precedent"]
            selected.append(exp.alias_(delta, f"{m}_evolution_pct"))
            out_cols.append(f"{m}_evolution_pct")
        final = exp.select(*selected).from_(exp.to_table("courant"))
        if dims:
            on = exp.and_(*[exp.EQ(this=exp.column(d.alias, table="courant"),
                                   expression=exp.column(d.alias, table="precedent")) for d in dims])
            join_type = "left" if self.write_dialect in NO_FULL_OUTER_JOIN else "full"
            final = final.join(exp.to_table("precedent"), on=on, join_type=join_type)
        else:
            final = final.join(exp.to_table("precedent"), join_type="cross")
        if hoisted:
            final.set(WITH_KEY, exp.With(expressions=hoisted))
        final = final.with_("courant", as_=current).with_("precedent", as_=previous)
        return final, dims, out_cols

    def compile(self, intent: Intent) -> Compiled:
        report = Compiled(sql="", dialect=self.write_dialect, ast=exp.Select(), columns=[])
        report.assumptions += list(intent.assumptions)
        self._report = report
        self.as_of = intent.time.start if intent.time else None
        if intent.escape_sql:
            return self._compile_escape(intent.escape_sql, report)
        if intent.time and intent.time.compare:
            query, dims, columns = self._compare(intent, report)
        else:
            query, dims, columns = self._measures_query(intent, intent.time, "", report)

        order: list[exp.Expression] = []
        if intent.ranking:
            by = intent.ranking.by or next((c for c in columns[len(dims):]), None)
            if intent.time and intent.time.compare and by and not by.endswith("_evolution_pct") \
                    and intent.time.compare_output == "pct":
                by = f"{by}_evolution_pct"
            if by not in columns:
                by = next((c for c in columns if c.startswith(by or "")), columns[-1])
            order.append(exp.Ordered(this=exp.column(by), desc=intent.ranking.direction == "desc"))
            order += [exp.Ordered(this=exp.column(d.alias)) for d in dims]
            if intent.ranking.limit:
                query = query.limit(min(intent.ranking.limit, self.policy.max_rows))
        elif any(d.grain for d in dims):
            order += [exp.Ordered(this=exp.column(d.alias)) for d in dims if d.grain]
        if order:
            query = query.order_by(*order)
        if not query.args.get(LIMIT_KEY) and dims:
            query = query.limit(self.policy.max_rows)

        check_tree(query)
        check_columns(query, self.policy)
        report.ast = query
        report.sql = query.sql(dialect=self.write_dialect, pretty=True)
        report.columns = columns
        report.signals["dims"] = len(dims)
        check_read_only(report.sql, self.write_dialect)
        report.signals.setdefault("fanout", False)
        report.signals.setdefault("ambiguous_join", False)
        return report

    def _compile_escape(self, sql: str, report: Compiled) -> Compiled:
        if not self.policy.allow_free_sql:
            raise PolicyViolation("le SQL libre est désactivé par la politique")
        if self.policy.row_filters:
            raise PolicyViolation("SQL libre interdit quand des filtres de lignes s'appliquent")
        tree = check_read_only(sql, self.read_dialect)
        check_columns(tree, self.policy)
        report.ast = tree
        report.sql = tree.sql(dialect=self.write_dialect, pretty=True)
        report.columns = [s.alias_or_name for s in tree.selects] if hasattr(tree, "selects") else []
        report.tables = sorted({t.name for t in tree.find_all(exp.Table)})
        report.signals.update({"escape": True, "fanout": False, "ambiguous_join": False})
        report.assumptions.append("requête en SQL libre : jointures et grain non vérifiés par le compilateur")
        return report
