"""Normalisation de texte pour l'appariement de vocabulaire (français)."""

from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9]+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def norm(text: str) -> str:
    """Minuscules, sans accents ni ponctuation ; « % » devient « pourcent »."""
    text = strip_accents(text.lower()).replace("%", " pourcent ")
    text = re.sub(r"[’'`]", " ", text)
    return _NON_WORD.sub(" ", text).strip()


# Mots terminés par s ou x qui ne sont pas des pluriels.
_INVARIABLE = frozenset({
    "mois", "mars", "pays", "fois", "prix", "poids", "temps", "cours", "corps", "bois", "choix", "plus", "moins",
    "sans", "dans", "chez", "sous", "vers", "tres", "apres", "depuis", "jamais", "toujours", "alors",
    "avons", "nous", "vous", "sont", "taux", "repas", "relais", "detail", "francais", "anglais",
})


def singular(token: str) -> str:
    """Singularise grossièrement un mot français (pluriel en -s ou -x)."""
    if token in _INVARIABLE:
        return token
    if len(token) > 3 and token.endswith(("s", "x")) and not token.endswith(("ss", "us")):
        return token[:-1]
    return token


def tokens(text: str) -> list[str]:
    return [singular(t) for t in norm(text).split()]


def phrase(text: str) -> str:
    """Forme canonique d'une expression : tokens normalisés et singularisés."""
    return " ".join(tokens(text))


# Mots grammaticaux et verbes courants qui n'apportent pas de sens à une question analytique.
STOPWORDS = frozenset(
    phrase(w)
    for w in """
    le la les l de des du d n j m t c s un une et en a au aux pour par sur dans quel quelle quels quelles
    est sont combien nombre donne donner moi nous notre nos avon avons avez ont ete etre qui que qu quoi
    ce cet cette ces leur leurs y il elle on mon ma mes son sa ses avec entre chaque selon
    tres bien fait faite faites liste lister affiche afficher montre montrer indique indiquer
    passe passee passees passes passer effectue effectuee effectuees realise realisee realisees
    total totale totaux global globale peux pouvez veux voudrais aimerais svp merci stp ou
    gagne gagnes gagnee gagnees obtenu obtenus identifie identifier chacun chacune
    situe situes situee situees base bases basee basees localise localises implante implantes
    debut sa son veuillez calcule calculer quelle-est c est
    """.split()
)

# Mots qui signalent une négation : non exprimable en QIR v0.
NEGATIONS = frozenset({"aucun", "aucune", "jamais", "sans", "pa", "ni", "nul", "nulle"})
