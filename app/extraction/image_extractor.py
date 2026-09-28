"""Image -> passenger rows via Gemini (one call per image)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from google.genai import types

from app.core.config import Settings
from app.core.costs import extract_prices
from app.core.models import GeminiExtraction

from .gemini_client import CallRecord, GeminiClient, GeminiResult

PROMPT_PATH = Path(__file__).parent / "prompts" / "extract_rows.txt"
SYSTEM_INSTRUCTION = ("You transcribe airline GDS screens exactly as displayed. "
                      "You never guess, autocomplete or correct characters.")


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def extract_image(
    client: GeminiClient,
    settings: Settings,
    image_bytes: bytes,
    mime_type: str,
    on_call: Callable[[CallRecord], None] | None = None,
) -> GeminiResult[GeminiExtraction]:
    contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), load_prompt()]
    return client.generate_json(
        contents,
        GeminiExtraction,
        model=settings.gemini_model_extract,
        prices=extract_prices(settings),
        purpose="extract_rows",
        thinking=settings.gemini_thinking_extract,
        system_instruction=SYSTEM_INSTRUCTION,
        on_call=on_call,
    )
