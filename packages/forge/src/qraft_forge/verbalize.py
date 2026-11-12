"""Verbalisation : intentions -> questions françaises (étape 4 de la Forge).

Deux sources de questions :
- TemplateVerbalizer : gabarits déterministes (graine), plusieurs formulations par motif,
  synonymes du glossaire tirés au hasard. Gratuit et instantané : c'est le socle du jeu.
- ClaudeParaphraser : prépare des requêtes de paraphrase pour l'API Message Batches du SDK
  anthropic (−50 %) et relit les résultats par custom_id, jamais par position.

Les mots employés sont choisis dans le vocabulaire du graphe (lexique) : une formulation
n'est proposée que si elle y désigne sans ambiguïté le nœud voulu. Quelle que soit leur
source, toutes les questions passent ensuite le filtre aller-retour.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import msgspec

from qraft.explain import Explainer
from qraft.graph import KnowledgeGraph, LexiconEntry
from qraft.qir import Dimension, Filter, Intent, Measure, TimeScope
from qraft.text import norm, phrase, singular, strip_accents

from .dataset import Pair

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]
ORDINALS = {1: "premier", 2: "deuxième", 3: "troisième", 4: "quatrième"}
GRAIN_NOUNS = {"day": ("jour",), "week": ("semaine",), "month": ("mois",), "quarter": ("trimestre",),
               "year": ("année", "an")}
GRAIN_ADJECTIVES = {"day": ("quotidien", "quotidienne"), "week": ("hebdomadaire", "hebdomadaire"),
                    "month": ("mensuel", "mensuelle"), "quarter": ("trimestriel", "trimestrielle"),
                    "year": ("annuel", "annuelle")}
EVOLUTION_WORDS = (("Évolution", "l'évolution"), ("Variation", "la variation"),
                   ("Progression", "la progression"), ("Croissance", "la croissance"))
# Natures de nœuds qui captent une formulation avant celle visée (ordre d'examen du parseur à règles).
_RIVALS = {"metric": (), "dimension": ("metric",), "entity": ("metric",), "value": ("metric", "dimension", "entity")}
# Synonymes qui ne sont pas des groupes nominaux (« plus vendus », « vendus en quantité »).
_NOT_A_NOUN = {"plus", "moins", "vendu", "vendus", "vendue", "vendues"}

# Genre grammatical : le glossaire ne le dit pas, on le devine (mots fréquents, puis terminaisons).
_FEMININE = {
    "vente", "valeur", "part", "marge", "somme", "moyenne", "commande", "remise", "recette", "depense", "facture",
    "clientele", "famille", "categorie", "region", "quantite", "date", "ligne", "reference", "annee", "semaine",
    "periode", "zone", "ville", "agence", "boutique", "enseigne", "gamme", "marque", "taille", "couleur",
    "livraison", "note", "duree", "transaction", "operation", "branche", "filiale", "division", "equipe",
}
_MASCULINE = {
    "chiffre", "nombre", "montant", "panier", "revenu", "volume", "produit", "client", "segment", "canal", "type",
    "article", "compte", "achat", "paiement", "reglement", "encaissement", "encaisse", "magasin", "service", "mois",
    "trimestre", "prix", "cout", "stock", "taux", "total", "pays", "secteur", "site", "fournisseur", "statut",
}
_FEMININE_ENDINGS = ("ion", "té", "ie", "ée", "ille", "elle", "ette", "ure", "ance", "ence", "ise", "ade", "ière",
                     "esse")


def _elides(text: str) -> bool:
    first = strip_accents(text.strip()[:1]).lower()
    return bool(first) and first in "aeiouyh"


def _de(text: str) -> str:
    """« de clients », « d'articles »."""
    return f"d'{text}" if _elides(text) else f"de {text}"


def pluralize(text: str) -> str:
    """Pluriel du premier mot : « région » -> « régions », « canal » -> « canaux », « type de client » -> « types de client »."""
    words = text.split(" ")
    first, low = words[0], words[0].lower()
    if low.endswith(("s", "x", "z")):
        plural = first
    elif low.endswith("al") and low not in {"bal", "carnaval", "festival", "récital", "régal"}:
        plural = first[:-2] + "aux"
    elif low.endswith(("eau", "au", "eu")):
        plural = first + "x"
    else:
        plural = first + "s"
    return " ".join([plural, *words[1:]])


@dataclass(frozen=True)
class Noun:
    """Groupe nominal avec ce qu'il faut pour l'accorder."""

    text: str
    feminine: bool = False
    plural: bool = False

    @classmethod
    def guess(cls, text: str, *, plural: bool | None = None) -> Noun:
        text = text.strip()
        first = re.split(r"[\s'’-]", text)[0]
        token = norm(first)
        is_plural = (bool(token) and singular(token) != token) if plural is None else plural
        base = singular(token) if is_plural else token
        low = first.lower()
        if is_plural and low.endswith(("s", "x")):
            low = low[:-1]
        if base in _FEMININE:
            feminine = True
        elif base in _MASCULINE:
            feminine = False
        else:
            feminine = low.endswith(_FEMININE_ENDINGS)
        return cls(text, feminine, is_plural)

    def det(self) -> str:
        """« le CA », « la quantité vendue », « l'encaissé », « les ventes »."""
        if self.plural:
            return f"les {self.text}"
        if _elides(self.text):
            return f"l'{self.text}"
        return f"{'la' if self.feminine else 'le'} {self.text}"

    def de(self) -> str:
        """« du CA », « de la quantité vendue », « de l'encaissé », « des ventes »."""
        if self.plural:
            return f"des {self.text}"
        if _elides(self.text):
            return f"de l'{self.text}"
        return f"{'de la' if self.feminine else 'du'} {self.text}"

    def agree(self, masc: str, fem: str, masc_plural: str, fem_plural: str) -> str:
        if self.plural:
            return fem_plural if self.feminine else masc_plural
        return fem if self.feminine else masc

    def extend(self, suffix: str) -> Noun:
        return Noun(f"{self.text} {suffix}", self.feminine, self.plural)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.])", r"\1", text)
    text = re.sub(r",\s*(,|\?|$)", r"\1", text)
    text = re.sub(r"\s*\?$", " ?", text).strip(" ,")
    return text[:1].upper() + text[1:]


class TemplateVerbalizer:
    """Questions françaises par gabarits, à partir de la structure de l'intention."""

    def __init__(self, graph: KnowledgeGraph, *, seed: int = 42) -> None:
        self.graph = graph
        self.rng = random.Random(seed)
        self.lexicon: dict[str, list[LexiconEntry]] = {}
        for entry in graph.lexicon():
            self.lexicon.setdefault(entry.phrase, []).append(entry)
        self.metric_words = {mid: self._metric_words(mid) for mid in graph.semantic.metrics}
        self.dim_words = {did: self._dim_words(did) for did in graph.semantic.dimensions}
        self.entity_words = {name: self._entity_words(name) for name in graph.catalog.tables}
        self.column_dims: dict[str, str] = {}
        for did in graph.semantic.dimensions:
            try:
                self.column_dims.setdefault(graph.dimension(did)[0].lower(), did)
            except ValueError:
                continue
        self._values: dict[tuple[str, str], list[tuple[str, bool]]] = {}

    # --- Vocabulaire -----------------------------------------------------------------------------

    def _means(self, text: str, kind: str, ref: str, value: str | None = None) -> bool:
        """text désigne sans ambiguïté (kind, ref) dans le lexique, sans être capté par un nœud prioritaire.

        Une métrique l'emporte toujours ; une dimension ou une entité de même libellé coexistent
        (le contexte « par » ou « combien de » les départage) ; une valeur doit être seule.
        """
        entries = self.lexicon.get(phrase(text), [])
        if any(e.kind in _RIVALS[kind] for e in entries):
            return False
        same = [e for e in entries if e.kind == kind]
        return bool(same) and all(e.ref == ref and (value is None or e.value == value) for e in same)

    def _metric_words(self, metric_id: str) -> list[Noun]:
        metric = self.graph.semantic.metrics[metric_id]
        words, seen = [], set()
        for text in [metric.label or metric_id.replace("_", " "), *metric.synonyms]:
            tokens = norm(text).split()
            if not tokens or tokens[0] in _NOT_A_NOUN or phrase(text) in seen:
                continue
            if norm(text).endswith("s") and phrase(norm(text)[:-1]) in seen:
                continue  # pluriel d'une forme déjà retenue (« revenus » après « revenu »)
            if self._means(text, "metric", metric_id):
                seen.add(phrase(text))
                words.append(Noun.guess(text))
        return words or [Noun.guess(metric.label or metric_id.replace("_", " "))]

    def _dim_words(self, dim_id: str) -> tuple[list[Noun], list[Noun]]:
        """(formes au singulier pour « par ... », formes au pluriel pour « top 5 ... »)."""
        dim = self.graph.semantic.dimensions[dim_id]
        texts = [dim.label or dim_id, *dim.synonyms, dim_id.replace("_", " ")]
        singulars, plurals, seen_s, seen_p = [], [], set(), set()
        for text in texts:
            if not text:
                continue
            if norm(text) == phrase(text) and phrase(text) not in seen_s and self._means(text, "dimension", dim_id):
                seen_s.add(phrase(text))
                singulars.append(Noun.guess(text, plural=False))
            candidate = text if norm(text) != phrase(text) else pluralize(text)
            if norm(candidate) not in seen_p and self._means(candidate, "dimension", dim_id):
                seen_p.add(norm(candidate))
                plurals.append(Noun.guess(candidate, plural=True))
        return singulars or [Noun.guess(dim.label or dim_id, plural=False)], plurals

    def _entity_words(self, table: str) -> list[str]:
        """Formes au pluriel d'une entité, pour « nombre de ... » et « combien de ... »."""
        entity = self.graph.semantic.entities.get(table)
        label = (entity.label if entity else None) or table.replace("_", " ")
        texts = [label if norm(label) != phrase(label) else pluralize(label)]
        texts += [s for s in (entity.synonyms if entity else []) if norm(s) != phrase(s)]
        words, seen = [], set()
        for text in texts:
            if norm(text) not in seen and self._means(text, "entity", table):
                seen.add(norm(text))
                words.append(text)
        return words

    def _value_words(self, column: str, value: Any) -> list[tuple[str, bool]]:
        """Formulations d'une valeur : (texte, est-ce la valeur brute)."""
        key = (column, str(value))
        if key not in self._values:
            words: list[tuple[str, bool]] = []
            found = self.graph.catalog.resolve(column)
            if found:
                ref = f"{found[0].name}.{found[1].name}"
                raw = str(value).replace("_", " ")
                if self._means(raw, "value", ref, str(value)):
                    words.append((raw, True))
                for alias in self.graph.semantic.values:
                    target = self.graph.catalog.resolve(alias.column)
                    if target and f"{target[0].name}.{target[1].name}" == ref and alias.value == str(value):
                        words += [(s, False) for s in alias.synonyms if self._means(s, "value", ref, str(value))]
            self._values[key] = list(dict.fromkeys(words))
        return self._values[key]

    def _column_noun(self, column: str) -> Noun | None:
        found = self.graph.catalog.resolve(column)
        dim_id = self.column_dims.get(f"{found[0].name}.{found[1].name}".lower()) if found else None
        return self.rng.choice(self.dim_words[dim_id][0]) if dim_id else None

    # --- Morceaux de phrase ----------------------------------------------------------------------

    def _measure(self, m: Measure) -> tuple[Noun, str | None] | None:
        """(groupe nominal de la mesure, entité au pluriel si c'est un comptage)."""
        if m.filters:
            return None
        if m.metric:
            words = self.metric_words.get(m.metric)
            return (self.rng.choice(words), None) if words else None
        if m.agg == "count" and m.column and "." not in m.column:
            table = self.graph.catalog.table(m.column)
            words = self.entity_words.get(table.name) if table else None
            if not words:
                return None
            entity = self.rng.choice(words)
            return Noun(f"nombre {_de(entity)}"), entity
        return None

    def _additive(self, m: Measure) -> bool:
        if not m.metric:
            return False
        metric = self.graph.semantic.metrics[m.metric]
        return not metric.is_composite and metric.expression.strip().upper().startswith(("SUM(", "COUNT("))

    def _filter(self, f: Filter) -> str | None:
        if f.op != "=" or isinstance(f.value, (list, tuple)) or f.value is None:
            return None
        words = self._value_words(f.column, f.value)
        if not words:
            return None
        text, raw = self.rng.choice(words)
        if not raw:
            return text  # synonyme du glossaire : « payées », « en ligne »
        options = [f"pour {text}"]
        if (label := self._column_noun(f.column)) is not None:
            options += [f"pour {label.det()} {text}", f"{label.de()} {text}"]
        return self.rng.choice(options)

    def _period(self, t: TimeScope | None, *, allow_range: bool = True) -> str | None:
        """Période en clair ; "" sans période ; None si la période n'a pas de formulation absolue."""
        if t is None or not (t.start or t.end):
            return ""
        if not (t.start and t.end) or t.column or t.label:
            return None
        s, e = date.fromisoformat(t.start), date.fromisoformat(t.end)
        if s.day != 1:
            return None
        if s.month == 1 and e == date(s.year + 1, 1, 1):
            return self.rng.choice([f"en {s.year}", f"sur {s.year}", f"pour {s.year}"])
        if s.month == 1 and e == date(s.year + 2, 1, 1) and allow_range:
            return f"entre {s.year} et {s.year + 1}"
        q = (s.month - 1) // 3 + 1
        quarter_end = date(s.year + 1, 1, 1) if q == 4 else date(s.year, 3 * q + 1, 1)
        if s.month in (1, 4, 7, 10) and e == quarter_end:
            return self.rng.choice([f"au T{q} {s.year}", f"au {ORDINALS[q]} trimestre {s.year}",
                                    f"sur le T{q} {s.year}"])
        if e == date(s.year + (s.month == 12), s.month % 12 + 1, 1):
            month = MONTHS[s.month - 1]
            return self.rng.choice([f"en {month} {s.year}", f"pour {month} {s.year}", f"sur {month} {s.year}"])
        return None

    def _dimension(self, d: Dimension) -> str | None:
        if d.column == "time":
            word = self.rng.choice(GRAIN_NOUNS[d.grain or "day"])
            options = [f"par {word}"] + ([f"chaque {word}"] if word != "an" else [])
            return self.rng.choice(options)
        if d.grain or d.column not in self.dim_words:
            return None
        noun = self.rng.choice(self.dim_words[d.column][0])
        return self.rng.choice([f"par {noun.text}", f"pour chaque {noun.text}", f"selon {noun.det()}"])

    def _dimensions(self, dims: Iterable[Dimension]) -> str | None:
        parts = [self._dimension(d) for d in dims]
        return None if any(p is None for p in parts) else " et ".join(parts)

    def _adjective(self, noun: Noun, grain: str) -> Noun:
        masc, fem = GRAIN_ADJECTIVES[grain]
        return noun.extend(noun.agree(masc, fem, masc + "s", fem + "s"))

    # --- Gabarits ----------------------------------------------------------------------------------

    def _plain(self, intent: Intent, measures: list[tuple[Noun, str | None]], f: str, p: str) -> str | None:
        if len(measures) == 2:
            (a, _), (b, _) = measures
            d = self._dimensions(intent.dimensions)
            if d is None:
                return None
            return self.rng.choice([
                f"{a.det()} et {b.det()} {f} {d} {p}",
                f"Quels sont {a.det()} et {b.det()} {f} {d} {p} ?",
                f"Donne-moi {a.det()} et {b.det()} {d} {f} {p}",
                f"{a.text} et {b.text} {d} {f} {p}",
            ])
        noun, entity = measures[0]
        d = self._dimensions(intent.dimensions)
        if d is None:
            return None
        templates = []
        last = intent.dimensions[-1] if intent.dimensions else None
        if last is not None and last.column == "time" and self.rng.random() < 0.4:
            # « chiffre d'affaires mensuel » : le grain devient un adjectif accordé.
            rest = self._dimensions(intent.dimensions[:-1])
            if rest is not None:
                noun, d = self._adjective(noun, last.grain or "day"), rest
                entity = None
        quel = noun.agree("Quel est", "Quelle est", "Quels sont", "Quelles sont")
        templates += [
            f"{noun.text} {f} {d} {p}",
            f"{quel} {noun.det()} {f} {d} {p} ?",
            f"Donne-moi {noun.det()} {f} {d} {p}",
            f"Montre-moi {noun.det()} {d} {f} {p}",
            f"Affiche {noun.det()} {f} {d} {p}",
            f"Calcule {noun.det()} {p} {f} {d}",
        ]
        if p:
            templates.append(f"{p}, {noun.det()} {f} {d} ?")
        if entity:
            templates += [f"Combien {_de(entity)} {f} {d} {p} ?", f"Combien {_de(entity)} avons-nous {f} {d} {p} ?"]
        return self.rng.choice(templates)

    def _ranked(self, intent: Intent, measures: list[tuple[Noun, str | None]], f: str, p: str) -> str | None:
        r = intent.ranking
        assert r is not None
        dims = intent.dimensions
        if len(measures) != 1 or len(dims) != 1 or dims[0].column == "time" or dims[0].grain \
                or dims[0].column not in self.dim_words:
            return None
        noun, entity = measures[0]
        singulars, plurals = self.dim_words[dims[0].column]
        one = self.rng.choice(singulars)
        desc = r.direction == "desc"
        tail = f"{f} {p}"
        superlative = noun.agree(*(("le plus élevé", "la plus élevée", "les plus élevés", "les plus élevées") if desc
                                   else ("le plus faible", "la plus faible", "les plus faibles", "les plus faibles")))
        if r.limit == 1:
            quel = one.agree("Quel", "Quelle", "Quels", "Quelles")
            templates = [f"{quel} {one.text} a {noun.det()} {superlative} {tail} ?"]
            if entity:
                templates.append(f"{quel} {one.text} a le plus {'grand' if desc else 'petit'} {noun.text} {tail} ?")
            elif self._additive(intent.measures[0]):
                templates.append(f"{quel} {one.text} a le {'plus' if desc else 'moins'} de {noun.text} {tail} ?")
            return self.rng.choice(templates)
        if r.limit:
            if not plurals:
                return None
            many, k = self.rng.choice(plurals), r.limit
            templates = [f"Top {k} {many.text} par {noun.text} {tail}" if desc
                         else f"Top {k} {many.text} par {noun.text} croissant {tail}"]
            if len(norm(many.text).split()) == 1:  # le parseur cherche « plus » juste après le nombre
                strong = many.agree(*(("forts", "fortes", "forts", "fortes") if desc
                                      else ("faibles", "faibles", "faibles", "faibles")))
                quels = many.agree("Quels", "Quelles", "Quels", "Quelles")
                templates += [f"Les {k} {many.text} les plus {strong} en {noun.text} {tail}",
                              f"{quels} sont les {k} {many.text} les plus {strong} en {noun.text} {tail} ?"]
            return self.rng.choice(templates)
        d = self.rng.choice([f"par {one.text}", f"pour chaque {one.text}"])
        order = self.rng.choice(["du plus fort au plus faible", "par ordre décroissant"] if desc
                                else ["du plus faible au plus fort", "par ordre croissant"])
        return self.rng.choice([f"{noun.text} {d} {tail}, {order}", f"{noun.det()} {d} {f} {p} {order}",
                                f"Donne-moi {noun.det()} {d} {tail} {order}"])

    def _compare(self, intent: Intent, measures: list[tuple[Noun, str | None]], f: str) -> str | None:
        t = intent.time
        assert t is not None
        if t.compare != "previous_year" or len(measures) != 1 or any(d.column == "time" or d.grain
                                                                        for d in intent.dimensions):
            return None
        period = self._period(t, allow_range=False)
        if not period:
            return None
        s, e = date.fromisoformat(t.start), date.fromisoformat(t.end)
        if s.month == 1 and e == date(s.year + 1, 1, 1) and self.rng.random() < 0.4:
            period = f"entre {s.year - 1} et {s.year}"
        d = self._dimensions(intent.dimensions)
        if d is None:
            return None
        noun, _ = measures[0]
        title, with_article = self.rng.choice(EVOLUTION_WORDS)
        if t.compare_output == "pct":
            unit = self.rng.choice([" en %", " en pourcentage"])
            title, with_article = title + unit, with_article + unit
        return self.rng.choice([
            f"{title} {noun.de()} {f} {d} {period}",
            f"{title} {noun.de()} {period} {f} {d} ?",
            f"Quelle est {with_article} {noun.de()} {f} {d} {period} ?",
        ])

    def _ratio(self, intent: Intent, p: str) -> str | None:
        if len(intent.ratios) != 1 or intent.measures or intent.filters:
            return None
        r = intent.ratios[0]
        num, den = r.numerator, r.denominator
        if not r.percent or not num.metric or num.metric != den.metric or den.filters or len(num.filters) != 1:
            return None
        f = num.filters[0]
        if f.op != "=" or isinstance(f.value, (list, tuple)):
            return None
        words = self._value_words(f.column, f.value)
        d = self._dimensions(intent.dimensions)
        if not words or d is None:
            return None
        text, raw = self.rng.choice(words)
        part_noun, whole = self.rng.choice(self.metric_words[num.metric]), self.rng.choice(self.metric_words[num.metric])
        if raw:
            options = [_de(text), f"{part_noun.de()} pour {text}"]
            if (label := self._column_noun(f.column)) is not None:
                options.append(f"{label.de()} {text}")
            part = self.rng.choice(options)
        else:
            part = f"{part_noun.de()} {text}"
        return self.rng.choice([
            f"Part {part} dans {whole.det()} {d} {p}",
            f"Quelle est la part {part} dans {whole.det()} {d} {p} ?",
            f"Part {part} dans {whole.det()} total {p} {d}, en %",
        ])

    def question(self, intent: Intent) -> str | None:
        """Une question tirée au hasard pour l'intention, ou None si aucun gabarit ne l'exprime."""
        if intent.escape_sql or not intent.answerable or intent.needs_context or intent.assumptions:
            return None
        p = "" if intent.time and intent.time.compare else self._period(intent.time)
        if p is None:
            return None
        if intent.ratios:
            text = self._ratio(intent, p)
            return _clean(text) if text else None
        if not intent.measures or len(intent.measures) > 2:
            return None
        measures = [self._measure(m) for m in intent.measures]
        filters = [self._filter(f) for f in intent.filters]
        if any(m is None for m in measures) or any(f is None for f in filters):
            return None
        f = " ".join(filters)  # type: ignore[arg-type]
        if intent.ranking:
            text = self._ranked(intent, measures, f, p)  # type: ignore[arg-type]
        elif intent.time and intent.time.compare:
            text = self._compare(intent, measures, f)  # type: ignore[arg-type]
        else:
            text = self._plain(intent, measures, f, p)  # type: ignore[arg-type]
        return _clean(text) if text else None

    def questions(self, intent: Intent, k: int = 3) -> list[str]:
        """Jusqu'à k questions distinctes (forme normalisée) pour une intention."""
        found: list[str] = []
        seen: set[str] = set()
        misses = 0
        for _ in range(8 * k):
            q = self.question(intent)
            if q is None:
                misses += 1
                if misses >= 3 and not found:
                    break
                continue
            if (key := phrase(q)) not in seen:
                seen.add(key)
                found.append(q)
                if len(found) >= k:
                    break
        return found


# --- Paraphrase par Claude (API Message Batches) ---------------------------------------------------

PARAPHRASE_INSTRUCTIONS = """Tu reformules des questions posées en français à un outil d'analyse de données d'entreprise.

Règles :
- Chaque reformulation garde exactement le même sens : mêmes mesures, mêmes regroupements, mêmes filtres, même période, même classement.
- Ne change ni les nombres, ni les dates, ni les valeurs citées (noms de catégories, de régions, de canaux...).
- Varie le registre : question directe, formulation elliptique, ton familier, jargon métier, petite faute de frappe occasionnelle.
- N'ajoute aucune condition, aucune négation, aucun calcul absent de la question d'origine.
- Réponds uniquement par un tableau JSON de chaînes, sans commentaire."""

_CUSTOM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_paraphrases(text: str, limit: int | None = None) -> list[str]:
    """Liste de questions dans une réponse : tableau JSON, sinon une question par ligne."""
    items: list[str] | None = None
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            items = [x for x in data if isinstance(x, str)] if isinstance(data, list) else None
        except json.JSONDecodeError:
            items = None
    if items is None:
        items = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"') for line in text.splitlines()]
    found, seen = [], set()
    for item in items:
        question = " ".join(item.split())
        if question and (key := phrase(question)) and key not in seen:
            seen.add(key)
            found.append(question)
    return found[:limit] if limit else found


class ClaudeParaphraser:
    """Paraphrases par Claude via l'API Message Batches (SDK officiel anthropic).

    Usage : submit(pairs) -> identifiant de lot ; plus tard, is_done(id) puis collect(id)
    -> {custom_id: [questions]} ; pairs_from(résultats) -> nouvelles paires à repasser
    au filtre aller-retour. Les résultats d'un lot arrivent dans n'importe quel ordre :
    on les rattache toujours par custom_id. L'index custom_id -> paire peut être sauvegardé
    (save_index / load_index) car un lot peut prendre jusqu'à 24 h.
    """

    def __init__(self, graph: KnowledgeGraph, *, client: Any | None = None, model: str = "claude-opus-5-5",
                 max_tokens: int = 8192, effort: str = "low", per_question: int = 3) -> None:
        self.graph = graph
        self._client = client
        self.model = model
        # La réflexion de Claude Opus 5.5 est toujours active et compte dans max_tokens : marge confortable.
        self.max_tokens = max_tokens
        self.effort = effort
        self.per_question = per_question
        self.explainer = Explainer(graph)
        self.index: dict[str, Pair] = {}
        self.failures: Counter[str] = Counter()
        glossary = [f"- {m.label or m.id} : {', '.join(m.synonyms)}" for m in graph.semantic.metrics.values()]
        glossary += [f"- {d.label or d.id} : {', '.join(d.synonyms)}" for d in graph.semantic.dimensions.values()]
        self.system = PARAPHRASE_INSTRUCTIONS + "\n\nVocabulaire du domaine (terme : synonymes) :\n" + "\n".join(glossary)

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _prompt(self, pair: Pair) -> str:
        return (f"Question d'origine : {pair.question}\n"
                f"Sens exact à préserver : {self.explainer.explain(pair.intent)}\n"
                f"Écris {self.per_question} reformulations différentes.")

    def build_requests(self, pairs: list[Pair]) -> list[dict[str, Any]]:
        """Requêtes du lot, au format de client.messages.batches.create(requests=...).

        Chaque élément équivaut à Request(custom_id=..., params=MessageCreateParamsNonStreaming(...)).
        """
        requests = []
        offset = len(self.index)
        for i, pair in enumerate(pairs, start=offset):
            custom_id = f"q{i:06d}-{pair.fingerprint}"
            assert _CUSTOM_ID.match(custom_id)
            self.index[custom_id] = pair
            requests.append({
                "custom_id": custom_id,
                "params": {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    # Préfixe commun à toutes les requêtes : mis en cache dès qu'il dépasse la taille minimale.
                    "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user", "content": self._prompt(pair)}],
                    "output_config": {"effort": self.effort},
                },
            })
        return requests

    def submit(self, pairs: list[Pair]) -> str:
        """Crée le lot et retourne son identifiant."""
        batch = self.client.messages.batches.create(requests=self.build_requests(pairs))
        return batch.id

    def is_done(self, batch_id: str) -> bool:
        return self.client.messages.batches.retrieve(batch_id).processing_status == "ended"

    def collect(self, batch_id: str) -> dict[str, list[str]]:
        """Paraphrases par custom_id. Les échecs (errored, canceled, expired, refus) sont comptés dans failures."""
        found: dict[str, list[str]] = {}
        for item in self.client.messages.batches.results(batch_id):
            custom_id = item.custom_id
            if custom_id not in self.index:
                self.failures["inconnu"] += 1
                continue
            outcome = item.result
            if outcome.type != "succeeded":
                self.failures[outcome.type] += 1
                continue
            message = outcome.message
            if getattr(message, "stop_reason", None) in ("refusal", "max_tokens"):
                self.failures[message.stop_reason] += 1
                continue
            text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
            questions = [q for q in parse_paraphrases(text, self.per_question)
                         if phrase(q) != phrase(self.index[custom_id].question)]
            if questions:
                found[custom_id] = questions
            else:
                self.failures["vide"] += 1
        return found

    def pairs_from(self, results: dict[str, list[str]]) -> list[Pair]:
        """Nouvelles paires (origine « claude »), rattachées à leur intention par custom_id."""
        pairs = []
        for custom_id in sorted(results):
            source = self.index.get(custom_id)
            if source is not None:
                pairs += [replace(source, question=q, origin="claude") for q in results[custom_id]]
        return pairs

    def save_index(self, path: str | Path) -> None:
        with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
            for custom_id, pair in self.index.items():
                handle.write(json.dumps({
                    "custom_id": custom_id, "question": pair.question, "intent": pair.intent.to_dict(),
                    "motif": pair.motif, "rows": pair.rows, "origin": pair.origin, "tables": list(pair.tables),
                }, ensure_ascii=False) + "\n")

    def load_index(self, path: str | Path) -> None:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    self.index[row["custom_id"]] = Pair(
                        question=row["question"], intent=msgspec.convert(row["intent"], Intent), motif=row["motif"],
                        rows=row["rows"], origin=row["origin"], tables=tuple(row["tables"]))
