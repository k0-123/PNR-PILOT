"""Shared Gemini wrapper: structured JSON output, timeouts, retries, token + cost accounting.

Retry policy (PLAN section 6):
- network errors, 408/429 and 5xx: exponential backoff, up to GEMINI_MAX_RETRIES retries
- JSON that fails to parse or fails the schema: retried once
Every attempt is reported through `on_call` because every attempt is billed.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.costs import Prices, cost_usd
from app.core.retry import backoff_delay

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)

PARSE_RETRIES = 1


class GeminiError(Exception):
    """A Gemini call failed permanently."""

    def __init__(self, message: str, *, attempts: int = 0, raw_text: str | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.raw_text = raw_text


class GeminiParseError(GeminiError):
    """Gemini answered, but not with valid JSON for the schema (even after the re-try)."""


class MalformedResponseError(Exception):
    pass


@dataclass
class CallRecord:
    """One billed attempt."""
    purpose: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    duration_ms: int
    success: bool
    error: str | None = None


@dataclass
class GeminiResult(Generic[M]):
    data: M
    raw_text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    duration_ms: int
    attempts: int


def thinking_config(level: str) -> types.ThinkingConfig:
    """"off" -> thinking_budget=0 (Flash models); otherwise a thinking level (required by Pro)."""
    if level == "off":
        return types.ThinkingConfig(thinking_budget=0)
    return types.ThinkingConfig(thinking_level=level)


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError):
        return exc.code in (408, 429)
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))


def is_rate_limited(exc: Exception) -> bool:
    return isinstance(exc, errors.ClientError) and exc.code == 429


def parse_structured(text: str | None, schema: type[M]) -> M:
    """Parse JSON text into `schema`. Tolerates ```json fences and a bare list for {"rows": [...]}."""
    if not text or not text.strip():
        raise MalformedResponseError("empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise MalformedResponseError(f"invalid JSON ({exc.msg} at pos {exc.pos})") from exc
    if isinstance(data, list) and "rows" in schema.model_fields:
        data = {"rows": data}
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise MalformedResponseError(f"JSON does not match schema ({exc.error_count()} error(s))") from exc


class GeminiClient:
    def __init__(self, settings: Settings, client: genai.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self._sleep = sleep
        # Called on every 429, so parallel callers can lower their concurrency.
        self.on_rate_limit: Callable[[], None] | None = None
        if client is None:
            key = settings.gemini_api_key.get_secret_value()
            if not key:
                raise GeminiError("GEMINI_API_KEY is not set (add it to .env)")
            client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=settings.gemini_timeout_seconds * 1000),
            )
        self._client = client

    def generate_json(
        self,
        contents: list,
        schema: type[M],
        *,
        model: str,
        prices: Prices,
        purpose: str,
        thinking: str = "off",
        system_instruction: str | None = None,
        on_call: Callable[[CallRecord], None] | None = None,
    ) -> GeminiResult[M]:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0,
            thinking_config=thinking_config(thinking),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        transient_retries = parse_retries = attempts = 0
        tot_in = tot_out = 0
        tot_cost = 0.0
        started = time.monotonic()
        last_text: str | None = None

        while True:
            attempts += 1
            t0 = time.monotonic()
            try:
                resp = self._client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as exc:  # noqa: BLE001 - classified below
                ms = _ms(t0)
                self._report(on_call, CallRecord(purpose, model, 0, 0, 0.0, ms, False, _short(exc)))
                if is_rate_limited(exc) and self.on_rate_limit:
                    self.on_rate_limit()
                if is_transient(exc) and transient_retries < self.settings.gemini_max_retries:
                    transient_retries += 1
                    delay = backoff_delay(transient_retries)
                    log.warning("Gemini transient error, retrying",
                                extra={"purpose": purpose, "error_type": type(exc).__name__,
                                       "attempt": attempts, "retry_in_s": round(delay, 1)})
                    self._sleep(delay)
                    continue
                raise GeminiError(f"{purpose}: {type(exc).__name__}: {_short(exc)}",
                                  attempts=attempts, raw_text=last_text) from exc

            ms = _ms(t0)
            t_in, t_out = _tokens(resp)
            cost = cost_usd(t_in, t_out, prices)
            tot_in, tot_out, tot_cost = tot_in + t_in, tot_out + t_out, tot_cost + cost
            last_text = getattr(resp, "text", None)
            try:
                data = parse_structured(last_text, schema)
            except MalformedResponseError as exc:
                self._report(on_call, CallRecord(purpose, model, t_in, t_out, cost, ms, False, str(exc)))
                if parse_retries < PARSE_RETRIES:
                    parse_retries += 1
                    log.warning("Gemini returned malformed JSON, retrying once",
                                extra={"purpose": purpose, "attempt": attempts})
                    continue
                raise GeminiParseError(f"{purpose}: {exc}", attempts=attempts, raw_text=last_text) from exc

            self._report(on_call, CallRecord(purpose, model, t_in, t_out, cost, ms, True))
            log.info("Gemini call ok", extra={
                "purpose": purpose, "model": model, "tokens_in": tot_in, "tokens_out": tot_out,
                "cost_usd": round(tot_cost, 6), "duration_ms": _ms(started), "attempts": attempts})
            return GeminiResult(data, last_text or "", model, tot_in, tot_out, tot_cost,
                                _ms(started), attempts)

    @staticmethod
    def _report(on_call, record: CallRecord) -> None:
        if on_call:
            on_call(record)


def _tokens(resp) -> tuple[int, int]:
    meta = getattr(resp, "usage_metadata", None)
    if meta is None:
        return 0, 0
    t_in = getattr(meta, "prompt_token_count", None) or 0
    # Thinking tokens (if any) are billed as output.
    t_out = (getattr(meta, "candidates_token_count", None) or 0) + (
        getattr(meta, "thoughts_token_count", None) or 0)
    return t_in, t_out


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _short(exc: Exception, limit: int = 300) -> str:
    msg = str(exc)
    return msg if len(msg) <= limit else msg[:limit] + "..."
