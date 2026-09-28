"""Statuses (single source of truth, see realplan.md section 9) and shared Pydantic models."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class JobStatus(StrEnum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    READY_FOR_LOOKUP = "READY_FOR_LOOKUP"
    LOOKING_UP = "LOOKING_UP"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ImageStatus(StrEnum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    EXTRACTED = "EXTRACTED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


class ExtractionStatus(StrEnum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


# Only these rows are ever sent to the website.
LOOKUP_ELIGIBLE = frozenset({ExtractionStatus.READY, ExtractionStatus.APPROVED})


class LookupStatus(StrEnum):
    """Per-row lookup state (realplan section 9). A human triggers every search in the browser
    extension; the extension captures the result page and the worker parses it."""
    NOT_STARTED = "NOT_STARTED"
    CLAIMED = "CLAIMED"          # leased to a staff member's extension
    CAPTURED = "CAPTURED"        # result page uploaded, waiting for the worker to parse it
    PARSED = "PARSED"            # result fields extracted (also: filled from a GDS PNR screen)
    NOT_FOUND = "NOT_FOUND"
    MISMATCH = "MISMATCH"        # result page showed another PNR/surname
    PARSE_ERROR = "PARSE_ERROR"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"          # CAPTCHA / access denied seen by the extension


# Finished with a result (success or a definite "no booking").
LOOKUP_DONE = frozenset({LookupStatus.PARSED, LookupStatus.NOT_FOUND})
# Finished without a result; "Retry failed rows" puts these back to NOT_STARTED.
LOOKUP_RETRYABLE = frozenset({LookupStatus.MISMATCH, LookupStatus.PARSE_ERROR, LookupStatus.SKIPPED,
                              LookupStatus.BLOCKED})


# ---- Gemini image-extraction response schema. No validators or constraints here, so the
# SDK can turn it into a response_schema; validation happens in validators.py. ----

class GeminiRow(BaseModel):
    line_no: str | None = None
    pax_count: int | None = None
    surname: str | None = None
    first_name: str | None = None
    title: str | None = None
    pnr: str | None = None
    class_code: str | None = None
    status: str | None = None
    date: str | None = None
    office_id: str | None = None
    surname_confidence: float | None = None
    pnr_confidence: float | None = None
    notes: str | None = None


class GeminiExtraction(BaseModel):
    rows: list[GeminiRow]


class ExtractedRow(BaseModel):
    """A row after normalisation and validation, ready to store."""

    line_no: str | None
    pax_count: int | None
    surname: str | None
    first_name: str | None
    title: str | None
    pnr: str | None
    class_code: str | None
    status: str | None
    date: str | None
    office_id: str | None
    surname_confidence: float | None
    pnr_confidence: float | None
    ai_notes: str | None
    issues: list[str]  # validation problems that force NEEDS_REVIEW
    warnings: list[str]  # format oddities worth showing, but not blocking
    extraction_status: ExtractionStatus

    @property
    def notes_text(self) -> str:
        return "; ".join(p for p in [*self.issues, *self.warnings, self.ai_notes or ""] if p)
