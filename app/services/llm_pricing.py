"""Per-model token prices shared by usage logging, the daily budget and the evaluation script."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

# USD / 1M tokens. Source: the Text tokens price table (standard) on each model's page, checked 2026-09-27.
PRICES_CHECKED = "2026-09-27"


@dataclass(frozen=True)
class ModelPrice:
    input: float
    cached: float
    output: float
    source: str


PRICES: dict[str, ModelPrice] = {
    "gpt-5.6-luna": ModelPrice(0.20, 0.02, 1.20, "https://developers.openai.com/api/docs/models/gpt-5.6-luna"),
    "gpt-5.4-nano": ModelPrice(0.20, 0.02, 1.25, "https://developers.openai.com/api/docs/models/gpt-5.4-nano"),
    "gpt-5.4-mini": ModelPrice(0.75, 0.075, 4.50, "https://developers.openai.com/api/docs/models/gpt-5.4-mini"),
    "gpt-5-nano": ModelPrice(0.05, 0.005, 0.40, "https://developers.openai.com/api/docs/models/gpt-5-nano"),
}


class UnknownModelPriceError(RuntimeError):
    pass


def price_for(model: str) -> ModelPrice:
    """Exact name first, then the longest listed prefix so dated snapshots ("gpt-5.6-luna-2026-08-01") match."""
    if model in PRICES:
        return PRICES[model]
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(f"{name}-"):
            return PRICES[name]
    raise UnknownModelPriceError(f"no price for model {model!r} — add it to app/services/llm_pricing.py PRICES")


def call_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """USD for one call. OpenAI counts reasoning tokens inside output_tokens, so they are billed at the output
    price here without being added a second time."""
    price = price_for(model)
    uncached = max(input_tokens - cached_tokens, 0)
    return (uncached * price.input + cached_tokens * price.cached + output_tokens * price.output) / 1_000_000


def validate_configured_models() -> None:
    """Called at startup so a model without a price fails loudly instead of being logged as free."""
    for variable, model in (("LLM_MODEL", settings.llm_model), ("ASSISTANT_MODEL", settings.assistant_model)):
        try:
            price_for(model)
        except UnknownModelPriceError as exc:
            raise UnknownModelPriceError(f"{variable}={model!r}: {exc}") from None
