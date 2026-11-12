import importlib.util
import json

import pytest

from qraft import Dimension, Intent, Measure, TimeScope
from qraft_forge import ExecutionReward
from qraft_forge.dataset import intent_json
from qraft_forge.reward import decode_intent
from qraft_forge.train import build_parser, main as train_main

REFERENCE = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="region"),),
                   time=TimeScope(start="2025-01-01", end="2026-01-01"))


@pytest.fixture(scope="module")
def reward(graph, source):
    return ExecutionReward(graph, source)


def test_reference_intent_scores_one(reward):
    text = intent_json(REFERENCE)
    assert reward(prompts=["CA 2025 par région"], completions=[text], reference=[text]) == [1.0]
    detail = reward.score(text, text)
    assert (detail.execution, detail.format, detail.length_penalty, detail.total) == (1.0, 0.1, 0.0, 1.0)


def test_equivalent_intent_scores_one(reward):
    # Autre écriture (colonne brute, JSON compact dans un bloc de code) : même résultat d'exécution.
    compact = {"measures": [{"metric": "ca"}], "dimensions": [{"column": "regions.nom"}],
               "time": {"start": "2025-01-01", "end": "2026-01-01"}}
    completion = "```json\n" + json.dumps(compact) + "\n```"
    assert decode_intent(completion) is not None
    assert reward.score(completion, intent_json(REFERENCE)).execution == 1.0


def test_wrong_intent_scores_zero(reward):
    wrong_year = Intent(measures=(Measure(metric="ca"),), dimensions=(Dimension(column="region"),),
                        time=TimeScope(start="2024-01-01", end="2025-01-01"))
    detail = reward.score(intent_json(wrong_year), REFERENCE)
    assert detail.execution == 0.0
    assert detail.total == pytest.approx(0.1)  # seul le bonus de format reste : JSON QIR valide
    unknown_metric = '{"measures": [{"metric": "marge"}]}'
    assert reward.score(unknown_metric, REFERENCE).total == 0.0
    assert reward.score("SELECT SUM(montant) FROM paiements", REFERENCE).total == 0.0
    assert reward(completions=["", "{pas du json"], reference=[REFERENCE, REFERENCE]) == [0.0, 0.0]


def test_conversational_completions_and_length_penalty(reward):
    text = intent_json(REFERENCE)
    chatty = text + "\n\nVoici l'intention demandée, en espérant qu'elle convienne parfaitement." * 4
    scores = reward(prompts=[None, None], completions=[[{"role": "assistant", "content": text}],
                                                       [{"role": "assistant", "content": chatty}]],
                    reference=[text, text], trainer_state=None)
    assert scores[0] == 1.0
    assert 0.9 <= scores[1] < 1.0  # pénalité légère, plafonnée
    assert reward.__name__ == "recompense_execution"


def test_reward_requires_reference_column(reward):
    with pytest.raises(ValueError):
        reward(completions=[intent_json(REFERENCE)])


def test_train_script_is_importable_without_gpu_stack():
    args = build_parser().parse_args(["--data", "jeu", "--base-model", "modele", "--out", "adapters", "--stage", "sft"])
    assert args.stage == "sft" and args.lora_r == 16
    if importlib.util.find_spec("torch") is None or importlib.util.find_spec("trl") is None:
        with pytest.raises(SystemExit, match="qraft-forge\\[train\\]"):
            train_main(["--data", "jeu", "--base-model", "modele", "--out", "adapters", "--stage", "sft"])
