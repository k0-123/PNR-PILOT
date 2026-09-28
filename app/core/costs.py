"""Token -> USD cost. Prices come from .env so they can change without code changes."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class Prices:
    input_per_m: float
    output_per_m: float


def cost_usd(tokens_in: int, tokens_out: int, prices: Prices) -> float:
    return (tokens_in * prices.input_per_m + tokens_out * prices.output_per_m) / 1_000_000


def extract_prices(settings: Settings) -> Prices:
    return Prices(settings.price_input_per_m, settings.price_output_per_m)


def result_prices(settings: Settings) -> Prices:
    return Prices(settings.price_result_input_per_m, settings.price_result_output_per_m)
