"""Booking details from GDS PNR display screenshots (fallback to extension lookups).

The user uploads screenshots of the full PNR display (e.g. Amadeus RT). The worker reads each
with Gemini (the extraction model: exact characters matter for ticket numbers), then matches
every passenger to the job's rows by PNR + surname and stores the result fields exactly like an
extension lookup would, so the final Excel is the same.

Only READY/APPROVED rows are filled; rows still needing review are left alone.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from google.genai import types
from pydantic import BaseModel, Field, create_model

from app import credits
from app.core.config import ResultFieldsConfig, Settings
from app.core.costs import extract_prices
from app.core.db import Database, now
from app.core.models import JobStatus, LookupStatus
from app.core.storage import LocalStorage
from app.extraction.gemini_client import (
    CallRecord,
    GeminiClient,
    GeminiError,
    MalformedResponseError,
    parse_structured,
)
from app.extraction.validators import normalise

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "extraction" / "prompts" / "extract_pnr_screen.txt"
ELIGIBLE = ("READY", "APPROVED")
# Job statuses that may be marked COMPLETED once every row has a result from screens.
COMPLETABLE = [JobStatus.AWAITING_REVIEW, JobStatus.READY_FOR_LOOKUP, JobStatus.LOOKING_UP,
               JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED, JobStatus.PAUSED]


# ------------------------------------------------------------------ schema
def build_screen_model(fields: ResultFieldsConfig) -> type[BaseModel]:
    return _build(tuple((f.key, f.label, f.description) for f in fields.fields))


@lru_cache(maxsize=16)
def _build(spec) -> type[BaseModel]:
    pax_defs = {
        "surname": (str | None, Field(None, description="Surname as in the name element")),
        "first_name": (str | None, Field(None, description="First name as in the name element, no title")),
        **{key: (str | None, Field(None, description=desc or label)) for key, label, desc in spec},
        "confidence": (float | None, Field(None, description="0-1 confidence for this passenger's values")),
    }
    passenger = create_model("ScreenPassenger", **pax_defs)
    booking = create_model(
        "ScreenBooking",
        pnr=(str | None, Field(None, description="6-character record locator")),
        pnr_confidence=(float | None, Field(None)),
        passengers=(list[passenger], Field(default_factory=list)),
    )
    return create_model("ScreenExtraction", bookings=(list[booking], Field(default_factory=list)))


def build_prompt(fields: ResultFieldsConfig) -> str:
    lines = "\n".join(f"- {f.key}: {f.label}" + (f" ({f.description})" if f.description else "")
                      for f in fields.fields)
    return PROMPT_PATH.read_text(encoding="utf-8").replace("{fields}", lines)


def extract_screen(client: GeminiClient, settings: Settings, fields: ResultFieldsConfig, image: bytes,
                   mime_type: str, on_call: Callable[[CallRecord], None] | None = None):
    return client.generate_json(
        [types.Part.from_bytes(data=image, mime_type=mime_type), build_prompt(fields)],
        build_screen_model(fields),
        model=settings.gemini_model_extract,
        prices=extract_prices(settings),
        purpose="extract_pnr_screen",
        thinking=settings.gemini_thinking_extract,
        system_instruction="You transcribe airline GDS screens exactly as displayed. You never guess.",
        on_call=on_call,
    )


# ----------------------------------------------------------------- matching
def _key(name: str | None) -> str:
    return (normalise(name) or "").replace(" ", "").replace("-", "").replace("'", "")


def _prefix_match(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def match_passenger(rows: list, surname: str | None, first_name: str | None = None) -> list:
    """The row (same PNR) that belongs to a passenger, as a 0/1-element list.

    Surname: exact (ignoring spaces/-/'), else a prefix match either way (GDS list screens truncate
    long names). Several rows with that surname (families): the first name decides, also by prefix.
    Still ambiguous -> the first candidate; callers remove assigned rows so it's one row per passenger.
    """
    want = _key(surname)
    if not want:
        return []
    cands = [r for r in rows if _key(r["effective_surname"]) == want] or \
            [r for r in rows if _prefix_match(_key(r["effective_surname"]), want)]
    if len(cands) > 1 and _key(first_name):
        by_first = [r for r in cands if _prefix_match(_key(r["effective_first_name"]), _key(first_name))]
        cands = by_first or cands
    return cands[:1]


def eligible_rows_by_pnr(db: Database, job_id: int) -> dict[str, list]:
    rows_by_pnr: dict[str, list] = {}
    for r in db.get_rows(job_id):
        if r["extraction_status"] in ELIGIBLE and r["effective_pnr"]:
            rows_by_pnr.setdefault(r["effective_pnr"], []).append(r)
    return rows_by_pnr


def apply_booking(db: Database, booking, candidates: list, *, threshold: float,
                  page_text_path: str | None = None, screenshot_path: str | None = None,
                  booking_keys: list[str] | None = None) -> tuple[int, list[str]]:
    """Store one booking's passengers on the matching rows among `candidates` (rows of that PNR).

    Each passenger fills at most one row (matched by surname, then first name for families).
    With `booking_keys`, rows of the PNR whose passenger isn't listed still get the booking-wide
    values (flights, route, contact, status). Returns (rows filled, unmatched passengers).
    Shared by GDS PNR screens and captured website pages, so both fill the Excel the same way.
    """
    pnr = (normalise(booking.pnr) or "").replace(" ", "")
    candidates = list(candidates)
    passengers = booking.passengers
    filled, unmatched = 0, []
    first_values: dict | None = None
    low_conf: list[float] = []

    def store(row, values: dict, note: str | None) -> None:
        nonlocal filled
        db.save_lookup_result(row["id"], result=values, page_text_path=page_text_path,
                              screenshot_path=screenshot_path)
        db.finish_row(row["id"], LookupStatus.PARSED, error=note, attempts=1)
        credits.charge_row(db, row["id"], row["job_id"])  # 1 FLT per row with a result, once
        filled += 1

    for pax in passengers:
        hits = match_passenger(candidates, pax.surname, pax.first_name)
        if not hits and len(passengers) == 1 and len(candidates) == 1:
            hits = candidates[:1]  # single passenger on both sides: trust the PNR
        for r in hits:
            candidates.remove(r)  # one row per passenger
        values = pax.model_dump(exclude={"surname", "first_name"})
        first_values = first_values or values
        if not hits:
            unmatched.append(f"{pnr or '?'} {pax.surname or '?'}/{pax.first_name or ''}".strip())
            continue
        conf = values.get("confidence")
        low = [x for x in (conf, booking.pnr_confidence) if x is not None and x < threshold]
        low_conf += low
        # Only real warnings go to the Error column; where the values came from is in "Source".
        note = f"check values: low confidence ({min(low):.2f})" if low else None
        for r in hits:
            store(r, values, note)

    if booking_keys is not None and first_values is not None:
        shared = {k: first_values.get(k) for k in booking_keys}
        shared["confidence"] = first_values.get("confidence")
        for r in candidates:
            store(r, shared, "passenger not listed on the page: booking details only")
    return filled, unmatched


def apply_extraction(db: Database, job_id: int, screen, data: BaseModel, threshold: float) -> tuple[int, list]:
    """Store results on matching rows. Returns (rows matched, unmatched passenger descriptions)."""
    rows_by_pnr = eligible_rows_by_pnr(db, job_id)
    matched, unmatched = 0, []
    for booking in data.bookings:
        pnr = (normalise(booking.pnr) or "").replace(" ", "")
        n, missing = apply_booking(db, booking, rows_by_pnr.get(pnr, []),
                                   threshold=threshold,
                                   screenshot_path=screen["stored_path"])
        matched += n
        unmatched += missing
    return matched, unmatched


def update_job_after_screens(db: Database, job_id: int) -> bool:
    """Mark the job COMPLETED once every eligible row has a result. Returns True if completed."""
    c = db.lookup_counts(job_id)
    if c["eligible"] == 0 or c[LookupStatus.PARSED] + c[LookupStatus.NOT_FOUND] < c["eligible"]:
        return False
    return db.transition_job(job_id, COMPLETABLE + [JobStatus.COMPLETED], JobStatus.COMPLETED,
                             lookup_finished_at=now(), lookup_requested=0, error_message=None)


def process_screen(db: Database, storage: LocalStorage, settings: Settings, client: GeminiClient,
                   fields: ResultFieldsConfig, screen) -> None:
    job_id, screen_id = screen["job_id"], screen["id"]
    ctx = {"job_id": job_id, "screen_id": screen_id}

    def on_call(rec: CallRecord) -> None:
        db.record_ai_call(job_id=job_id, purpose=rec.purpose, model=rec.model, tokens_in=rec.tokens_in,
                          tokens_out=rec.tokens_out, cost_usd=rec.cost_usd, duration_ms=rec.duration_ms,
                          success=rec.success, error=rec.error)

    # Reuse an identical screenshot read before (same SHA-256), so a re-upload or a re-run doesn't
    # pay Gemini again. Fields may have changed since; the model is all-optional, so parsing a stale
    # response just leaves the new fields empty. A malformed cache entry falls through to a real call.
    data = raw_text = None
    tokens_in = tokens_out = 0
    cost_usd = 0.0
    cached = db.find_cached_screen_extraction(screen["sha256"])
    if cached is not None:
        try:
            data = parse_structured(cached, build_screen_model(fields))
            raw_text = cached
            log.info("PNR screen read from cache", extra=ctx)
        except MalformedResponseError:
            data = None  # make a fresh call instead

    if data is None:
        try:
            image = storage.read_bytes(screen["stored_path"])
            res = extract_screen(client, settings, fields, image, screen["mime_type"], on_call=on_call)
        except (GeminiError, OSError) as exc:
            db.finish_pnr_screen(screen_id, status="FAILED", error=str(exc)[:500],
                                 raw=getattr(exc, "raw_text", None))
            log.error("PNR screen failed", extra={**ctx, "error": str(exc)[:200]})
            return
        data, raw_text = res.data, res.raw_text
        tokens_in, tokens_out, cost_usd = res.tokens_in, res.tokens_out, res.cost_usd

    matched, unmatched = apply_extraction(db, job_id, screen, data, settings.confidence_threshold)
    pnrs = ", ".join(sorted({(b.pnr or "?") for b in data.bookings}))
    error = None
    if not data.bookings:
        error = "no PNR display recognised in this image"
    elif not matched:
        error = "no passenger matched a row of this job (check PNR/surname, or review flagged rows first)"
    db.finish_pnr_screen(screen_id, status="DONE" if matched else "FAILED", pnrs=pnrs, matched_rows=matched,
                         unmatched=unmatched, raw=raw_text, tokens_in=tokens_in,
                         tokens_out=tokens_out, cost_usd=cost_usd, error=error)
    log.info("PNR screen processed", extra={**ctx, "bookings": len(data.bookings), "matched_rows": matched,
                                            "unmatched": len(unmatched), "cost_usd": round(cost_usd, 6),
                                            "from_cache": raw_text is cached})
    update_job_after_screens(db, job_id)


def unmatched_list(screen) -> list[str]:
    try:
        return json.loads(screen["unmatched"] or "[]")
    except (TypeError, ValueError):
        return []
