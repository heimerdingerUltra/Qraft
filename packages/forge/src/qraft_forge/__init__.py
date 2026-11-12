"""Forge Qraft : compile une base en paires question -> QIR vérifiées par exécution, puis en adapter LoRA."""

from .checks import ExecutionChecker, Reference, Rejected, RoundTrip, model_interpreter, rule_interpreter
from .dataset import Pair, dataset_stats, split_by_intent, write_dataset
from .pipeline import ForgeConfig, ForgeRun, forge, roundtrip_pairs
from .reward import ExecutionReward
from .sampler import IntentSampler, Sample
from .verbalize import ClaudeParaphraser, TemplateVerbalizer

__version__ = "0.1.0"

__all__ = [
    "ClaudeParaphraser", "ExecutionChecker", "ExecutionReward", "ForgeConfig", "ForgeRun", "IntentSampler", "Pair",
    "Reference", "Rejected", "RoundTrip", "Sample", "TemplateVerbalizer", "dataset_stats", "forge",
    "model_interpreter", "roundtrip_pairs", "rule_interpreter", "split_by_intent", "write_dataset",
]
