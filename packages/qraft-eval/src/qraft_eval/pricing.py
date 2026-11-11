"""Estimation du coût des appels au modèle (tarifs publics approximatifs, en dollars)."""

from __future__ import annotations

# Dollars par million de jetons. Tarifs publics de novembre 2026, à vérifier avant tout budget.
# cache_write_1h : écriture en cache avec une durée de vie d'une heure (environ 2 fois l'entrée).
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write_1h": 8.00},
}


def estimate_cost(
    model: str | None,
    *,
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> float | None:
    """Coût estimé en dollars, ou None si le modèle n'est pas dans la table."""
    prices = PRICES_PER_MTOK.get(model or "")
    if prices is None:
        return None
    return (
        input_tokens * prices["input"]
        + cache_read_tokens * prices["cache_read"]
        + cache_write_tokens * prices["cache_write_1h"]
        + output_tokens * prices["output"]
    ) / 1_000_000
