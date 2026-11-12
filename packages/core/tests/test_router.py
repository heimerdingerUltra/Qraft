from qraft import Engine, Intent, Measure
from qraft.models import StaticModel
from qraft.parser import RuleParser
from qraft.router import LearnedRouter, features

from .conftest import SEMANTIC

GOOD = ["Chiffre d'affaires 2025 par région", "Combien de clients avons-nous ?", "Panier moyen en 2025",
        "Quantité vendue par produit en 2024", "Montant encaissé par segment en 2025"]
BAD = ["Chiffre d'affaires des clients fidèles et rentables en 2025", "Ventes des produits phares saisonniers",
       "Quantité vendue aux clients premium récurrents", "Panier moyen des acheteurs impulsifs nocturnes"]


def test_router_learns_to_distrust_unexplained_words(graph, tmp_path):
    parser = RuleParser(graph)
    router = LearnedRouter.train(parser, [(q, True) for q in GOOD] + [(q, False) for q in BAD], threshold=0.6)
    assert all(router.trust_rules(parser.parse(q), q) for q in GOOD)
    assert not any(router.trust_rules(parser.parse(q), q) for q in BAD)
    reloaded = LearnedRouter.load(router.save(tmp_path / "routeur.json"))
    q = GOOD[0]
    assert reloaded.predict(features(parser.parse(q), q)) == router.predict(features(parser.parse(q), q))


def test_engine_sends_distrusted_questions_to_the_model(demo_url, graph):
    parser = RuleParser(graph)
    router = LearnedRouter.train(parser, [(q, True) for q in GOOD] + [(q, False) for q in BAD], threshold=0.6)
    question = BAD[0]
    model = StaticModel({question: [Intent(measures=(Measure(metric="ca"),))]})
    engine = Engine.from_url(demo_url, semantic=SEMANTIC, model=model, router=router)
    assert engine.ask(question).route == "modele"
    assert engine.ask(GOOD[0]).route == "regles"
