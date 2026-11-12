"""Graphe de connaissance : catalogue + couche sémantique + relations entre tables.

Il répond aux questions du compilateur et du parseur : quelle colonne porte le temps
d'une table, comment relier deux tables, quel mot désigne quelle métrique ou valeur.
"""

from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import msgspec

from .catalog import Catalog, Column, Table
from .semantic import Dimension, Metric, SemanticLayer
from .text import phrase


class CompileError(ValueError):
    """L'intention ne peut pas être compilée avec ce graphe."""


@dataclass(frozen=True)
class Edge:
    """Relation plusieurs-vers-un : child.child_col référence parent.parent_col."""

    child: str
    child_col: str
    parent: str
    parent_col: str
    origin: str = "declared"
    confidence: float = 1.0

    @property
    def weight(self) -> float:
        penalty = 0.0 if self.origin in ("declared", "semantic") else 0.25
        return 1.0 / max(self.confidence, 0.1) + penalty

    def other(self, table: str) -> str:
        return self.parent if table == self.child else self.child


@dataclass(frozen=True)
class Step:
    """Passage d'une table déjà jointe (source) à une nouvelle table (target)."""

    source: str
    target: str
    edge: Edge

    @property
    def fanout(self) -> bool:
        """True si l'on descend d'un parent vers ses enfants (relation un-vers-plusieurs)."""
        return self.source == self.edge.parent and self.target == self.edge.child


@dataclass
class JoinPlan:
    base: str
    steps: list[Step] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)

    @property
    def tables(self) -> list[str]:
        return [self.base] + [s.target for s in self.steps]

    @property
    def inferred_edges(self) -> int:
        return sum(1 for s in self.steps if s.edge.origin == "inferred")

    def subtree(self, root: str) -> set[str]:
        """Tables atteintes depuis root dans le plan (root compris)."""
        found, frontier = {root}, [root]
        while frontier:
            current = frontier.pop()
            for s in self.steps:
                if s.source == current and s.target not in found:
                    found.add(s.target)
                    frontier.append(s.target)
        return found


@dataclass(frozen=True)
class LexiconEntry:
    phrase: str  # forme canonique (tokens normalisés)
    kind: str  # metric | dimension | entity | value
    ref: str  # id de métrique/dimension, nom de table, ou « table.colonne »
    value: str | None = None  # valeur réelle pour kind == value


class KnowledgePack(msgspec.Struct):
    """Artefact versionné produit par l'indexation : ce que le moteur charge au démarrage."""

    catalog: Catalog
    semantic: SemanticLayer | None = None
    built_at: str = ""

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(msgspec.json.format(msgspec.json.encode(self), indent=1))
        return path

    @classmethod
    def load(cls, path: str | Path) -> KnowledgePack:
        return msgspec.json.decode(Path(path).read_bytes(), type=cls)


_TIME_HINTS = ("date", "jour", "created", "cree", "timestamp", "heure", "mois", "periode")


class KnowledgeGraph:
    def __init__(self, catalog: Catalog, semantic: SemanticLayer | None = None) -> None:
        self.catalog = catalog
        self.semantic = semantic or SemanticLayer()
        self.dialect = self.semantic.dialect or catalog.dialect
        self.edges = self._build_edges()
        self._adjacency: dict[str, list[Edge]] = {}
        for e in self.edges:
            self._adjacency.setdefault(e.child, []).append(e)
            self._adjacency.setdefault(e.parent, []).append(e)
        self.version = hashlib.sha1(
            msgspec.json.encode(catalog) + msgspec.json.encode(self.semantic)
        ).hexdigest()[:12]

    @classmethod
    def from_pack(cls, pack: KnowledgePack) -> KnowledgeGraph:
        return cls(pack.catalog, pack.semantic)

    def to_pack(self) -> KnowledgePack:
        return KnowledgePack(catalog=self.catalog, semantic=self.semantic,
                             built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # --- Structure -------------------------------------------------------------------------

    def _build_edges(self) -> list[Edge]:
        edges: dict[tuple[str, str, str, str], Edge] = {}
        for tbl in self.catalog.tables.values():
            for fk in tbl.foreign_keys:
                ref = self.catalog.table(fk.ref_table)
                if ref is None:
                    continue
                key = (tbl.name, fk.column, ref.name, fk.ref_column)
                edges[key] = Edge(tbl.name, fk.column, ref.name, fk.ref_column, fk.origin, fk.confidence)
        for rel in self.semantic.relationships:
            child = self.catalog.resolve(rel.from_)
            parent = self.catalog.resolve(rel.to)
            if child and parent:
                key = (child[0].name, child[1].name, parent[0].name, parent[1].name)
                edges[key] = Edge(*key, origin="semantic", confidence=1.0)
        return sorted(edges.values(), key=lambda e: (e.child, e.child_col, e.parent, e.parent_col))

    def table(self, name: str) -> Table:
        tbl = self.catalog.table(name)
        if tbl is None:
            raise CompileError(f"table inconnue : {name}")
        return tbl

    def column(self, ref: str) -> tuple[Table, Column]:
        found = self.catalog.resolve(ref)
        if found is None:
            raise CompileError(f"colonne inconnue : {ref}")
        return found

    def metric(self, metric_id: str) -> Metric:
        metric = self.semantic.metrics.get(metric_id)
        if metric is None:
            raise CompileError(f"métrique inconnue : {metric_id}")
        return metric

    def dimension(self, ref: str) -> tuple[str, Dimension | None]:
        """Résout une dimension (id du glossaire ou « table.colonne ») en « table.colonne »."""
        dim = self.semantic.dimensions.get(ref)
        if dim is not None:
            tbl, col = self.column(dim.column)
            return f"{tbl.name}.{col.name}", dim
        tbl, col = self.column(ref)
        return f"{tbl.name}.{col.name}", None

    def time_column(self, table_name: str) -> str | None:
        """Colonne temporelle d'une table : déclarée dans la couche sémantique, sinon devinée."""
        entity = self.semantic.entities.get(table_name)
        if entity and entity.time_column:
            tbl, col = self.column(entity.time_column if "." in entity.time_column
                                   else f"{table_name}.{entity.time_column}")
            return f"{tbl.name}.{col.name}"
        tbl = self.catalog.table(table_name)
        if tbl is None:
            return None
        temporal = [c for c in tbl.columns if c.is_temporal]
        hinted = [c for c in temporal if any(h in c.name.lower() for h in _TIME_HINTS)]
        chosen = (hinted or temporal or [None])[0]
        return f"{tbl.name}.{chosen.name}" if chosen else None

    def label(self, kind: str, ref: str) -> str:
        if kind == "metric" and ref in self.semantic.metrics:
            return self.semantic.metrics[ref].label or ref
        if kind == "dimension" and ref in self.semantic.dimensions:
            return self.semantic.dimensions[ref].label or ref
        if kind == "entity" and ref in self.semantic.entities:
            return self.semantic.entities[ref].label or ref
        return ref.split(".")[-1].replace("_", " ")

    # --- Planification des jointures ---------------------------------------------------------

    def _dijkstra(self, sources: set[str]) -> tuple[dict[str, float], dict[str, tuple[str, Edge]], set[str]]:
        dist = {s: 0.0 for s in sources}
        prev: dict[str, tuple[str, Edge]] = {}
        multiple: set[str] = set()
        heap = [(0.0, s) for s in sorted(sources)]
        heapq.heapify(heap)
        while heap:
            d, node = heapq.heappop(heap)
            if d > dist.get(node, float("inf")):
                continue
            for edge in sorted(self._adjacency.get(node, []), key=lambda e: (e.weight, e.child_col, e.parent_col)):
                nxt = edge.other(node)
                nd = d + edge.weight
                known = dist.get(nxt, float("inf"))
                if nd < known - 1e-9:
                    dist[nxt], prev[nxt] = nd, (node, edge)
                    multiple.discard(nxt)
                    if node in multiple:
                        multiple.add(nxt)
                    heapq.heappush(heap, (nd, nxt))
                elif abs(nd - known) <= 1e-9 and nxt not in sources and prev.get(nxt, (None, None))[1] != edge:
                    multiple.add(nxt)
        return dist, prev, multiple

    def plan_joins(self, base: str, required: set[str]) -> JoinPlan:
        """Arbre de jointure minimal reliant base aux tables requises (approximation de Steiner).

        On ajoute à chaque tour le chemin le plus court vers la table requise la plus proche.
        Deux chemins de même coût vers une table sont signalés comme ambiguïté.
        """
        plan = JoinPlan(base=base)
        tree = {base}
        remaining = set(required) - tree
        while remaining:
            dist, prev, multiple = self._dijkstra(tree)
            reachable = sorted((dist[t], t) for t in remaining if t in dist)
            if not reachable:
                missing = ", ".join(sorted(remaining))
                raise CompileError(f"aucune relation connue ne relie {base} à : {missing}")
            target = reachable[0][1]
            if target in multiple:
                plan.ambiguous.append(target)
            path: list[Step] = []
            node = target
            while node not in tree:
                source, edge = prev[node]
                path.append(Step(source, node, edge))
                node = source
            for step in reversed(path):
                plan.steps.append(step)
                tree.add(step.target)
            remaining -= tree
        return plan

    # --- Vocabulaire ---------------------------------------------------------------------------

    def lexicon(self) -> list[LexiconEntry]:
        entries: list[LexiconEntry] = []

        def add(text: str | None, kind: str, ref: str, value: str | None = None) -> None:
            if text and (p := phrase(text)):
                entries.append(LexiconEntry(p, kind, ref, value))

        for m in self.semantic.metrics.values():
            for text in [m.id.replace("_", " "), m.label, *m.synonyms]:
                add(text, "metric", m.id)
        for d in self.semantic.dimensions.values():
            for text in [d.id.replace("_", " "), d.label, *d.synonyms]:
                add(text, "dimension", d.id)
        for tbl in self.catalog.tables.values():
            entity = self.semantic.entities.get(tbl.name)
            for text in [tbl.name.replace("_", " "), *(entity and [entity.label, *entity.synonyms] or [])]:
                add(text, "entity", tbl.name)
            for col in tbl.columns:
                for value in col.values:
                    add(value, "value", f"{tbl.name}.{col.name}", value)
        for alias in self.semantic.values:
            found = self.catalog.resolve(alias.column)
            if found:
                ref = f"{found[0].name}.{found[1].name}"
                for text in [alias.value, *alias.synonyms]:
                    add(text, "value", ref, alias.value)
        # Les métriques et dimensions priment sur les entités et valeurs de même libellé.
        priority = {"metric": 0, "dimension": 1, "entity": 2, "value": 3}
        unique: dict[tuple[str, str, str, str | None], LexiconEntry] = {}
        for e in sorted(entries, key=lambda e: (priority[e.kind], e.phrase, e.ref)):
            unique.setdefault((e.phrase, e.kind, e.ref, e.value), e)
        return list(unique.values())

    def summary_for_prompt(self) -> str:
        """Description stable et triée du domaine, pour le prompt d'un modèle (préfixe mis en cache)."""
        lines = [f"Dialecte SQL : {self.dialect}", "", "Métriques certifiées :"]
        for m in sorted(self.semantic.metrics.values(), key=lambda m: m.id):
            syn = f" (synonymes : {', '.join(m.synonyms)})" if m.synonyms else ""
            lines.append(f"- {m.id} : {m.label or m.id}{syn}")
        lines += ["", "Dimensions :"]
        for d in sorted(self.semantic.dimensions.values(), key=lambda d: d.id):
            lines.append(f"- {d.id} : {d.label or d.id} -> {d.column}")
        lines += ["", "Tables :"]
        for tbl in self.catalog.tables.values():
            cols = ", ".join(f"{c.name} {c.type}" + (f" [{', '.join(c.values[:8])}]" if c.values else "")
                             for c in tbl.columns)
            lines.append(f"- {tbl.name}({cols})")
        lines += ["", "Relations (plusieurs -> un) :"]
        lines += [f"- {e.child}.{e.child_col} -> {e.parent}.{e.parent_col}" for e in self.edges]
        return "\n".join(lines)
