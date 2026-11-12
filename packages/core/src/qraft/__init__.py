"""Qraft : questions en français -> réponses chiffrées, vérifiées et expliquées, sur toute base SQL."""

from .compiler import Compiled, Compiler
from .engine import Answer, Engine, JsonlEventSink, build_graph
from .execute import Result, Source
from .graph import CompileError, KnowledgeGraph, KnowledgePack
from .policy import Identity, Policy, PolicyViolation, UnsafeSQL
from .qir import Dimension, Filter, Intent, Measure, Ranking, Ratio, TimeScope

__version__ = "0.1.0"

__all__ = [
    "AnalystAgent", "Answer", "ArrowSource", "Compiled", "Compiler", "CompileError", "Dimension", "Engine", "Filter",
    "Identity", "Intent", "JsonlEventSink", "KnowledgeGraph", "KnowledgePack", "LearnedRouter", "Measure", "Monitor",
    "Policy", "PolicyViolation", "Ranking", "Ratio", "Result", "Source", "TimeScope", "UnsafeSQL", "build_graph",
    "detect_anomalies", "explain_change", "federated_engine", "run_monitors",
]


def __getattr__(name: str):
    """Imports paresseux des modules optionnels (agent, insights, fédération, routeur)."""
    lazy = {
        "AnalystAgent": ("agent", "AnalystAgent"), "explain_change": ("insights", "explain_change"),
        "detect_anomalies": ("insights", "detect_anomalies"), "run_monitors": ("insights", "run_monitors"),
        "Monitor": ("insights", "Monitor"), "federated_engine": ("federation", "federated_engine"),
        "LearnedRouter": ("router", "LearnedRouter"), "ArrowSource": ("execute", "ArrowSource"),
    }
    if name in lazy:
        import importlib

        module, attr = lazy[name]
        return getattr(importlib.import_module(f".{module}", __name__), attr)
    raise AttributeError(f"module 'qraft' has no attribute {name!r}")
