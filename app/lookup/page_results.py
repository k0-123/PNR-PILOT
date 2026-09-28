"""Captured website pages -> result columns (newplan step B).

The extension uploads the text of each result page (one per unique PNR). The worker reads it
with Gemini (the result model, thinking off) using the same schema, prompt rules and passenger
matching as GDS PNR screens (`gds_screens`), so both routes fill the final Excel identically:

- per-passenger values (name, e-ticket, frequent flyer) go to that passenger's row, matched by
  surname and, for families, first name;
- booking-wide values (contact, from/to, flights, status) go to every row with that PNR, also
  rows whose passenger isn't listed on the page;
- a page that shows another booking reference -> MISMATCH (nothing stored);
- nothing readable / Gemini failure -> PARSE_ERROR. The page text is kept, so "Retry failed"
  reads it again without a new website search.

Gemini calls run in threads (`call`); every database write happens in `apply`, on the caller's
thread.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config import ResultFieldsConfig, Settings
from app.core.costs import result_prices
from app.core.db import ELIGIBLE_SQL, Database, ago, now
from app.core.logging_setup import mask
from app.core.models import JobStatus, LookupStatus
from app.core.storage import LocalStorage, StorageError
from app.extraction.gemini_client import CallRecord, GeminiClient, GeminiError, GeminiResult
from app.extraction.validators import normalise

from .gds_screens import apply_booking, build_prompt, build_screen_model, eligible_rows_by_pnr

log = logging.getLogger(__name__)

# Web pages often draw the title in its own element right before the name, so the page text reads
# "MsLijing Chen". Put the space back (only a known title directly followed by a capital letter).
_GLUED_TITLE = re.compile(r"^(Mr|Mrs|Ms|Miss|Mstr|Master|Dr|Prof)(?=[A-Z][a-z])")


def split_glued_title(name: str | None) -> str | None:
    return _GLUED_TITLE.sub(lambda m: m.group(1) + " ", name.strip()) if name else name


STALE_PARSE_SECONDS = 600  # a claim older than this (worker died) can be taken again
MIN_TEXT_CHARS = 40

PAGE_CONTEXT = """
The document is the visible text of an airline "manage booking" web page, captured right after
searching booking reference "{pnr}" for last name "{surname}".
Passengers we expect on this booking: {passengers}.
Return the booking shown on the page, with its booking reference exactly as the page shows it
(even if it is not {pnr}). If the page shows no booking at all, return {{"bookings": []}}.
The text may end with a part headed "=== TEXT FROM COLLAPSED / HIDDEN SECTIONS ===": sections of
the same page that were folded away (passenger details, e-tickets, contact, frequent flyer). Use
it to fill fields the visible part doesn't show, but only details that clearly belong to this
booking and its passengers; ignore menus, help texts and examples. When both parts show a value,
the visible part wins.
"""


@dataclass
class Capture:
    """A claimed capture plus what the Gemini call needs (read on the main thread)."""
    job_id: int
    pnr: str
    captures: int  # capture counter at claim time: a re-capture meanwhile makes this result stale
    page_text_path: str
    text: str = ""
    surname: str = ""
    passengers: list[str] = field(default_factory=list)
    error: str | None = None


# ------------------------------------------------------------------ claim
def claim(db: Database, worker_id: str, limit: int) -> list[Capture]:
    """Atomically take up to `limit` CAPTURED lookups for this worker, oldest first."""
    ts = now()
    with db.write_tx() as c:
        rows = c.execute(
            """UPDATE lookups SET parse_worker=?, parse_started_at=?
               WHERE rowid IN (SELECT rowid FROM lookups WHERE status='CAPTURED'
                               AND (parse_worker IS NULL OR parse_started_at < ?)
                               ORDER BY captured_at LIMIT ?)
               RETURNING job_id, pnr, captures, page_text_path""",
            (worker_id, ts, ago(STALE_PARSE_SECONDS), limit)).fetchall()
    return [Capture(r["job_id"], r["pnr"], r["captures"], r["page_text_path"]) for r in rows]


def prepare(db: Database, storage: LocalStorage, cap: Capture) -> Capture:
    """Read the saved page text and the passengers expected on this PNR."""
    try:
        cap.text = storage.read_bytes(cap.page_text_path).decode("utf-8", errors="replace")
    except (OSError, StorageError, TypeError) as exc:
        cap.error = f"saved page text is missing: {exc}"
        return cap
    rows = eligible_rows_by_pnr(db, cap.job_id).get(cap.pnr, [])
    cap.surname = rows[0]["effective_surname"] if rows else ""
    cap.passengers = [f"{r['effective_surname'] or '?'}/{r['effective_first_name'] or ''}" for r in rows]
    if len(cap.text.strip()) < MIN_TEXT_CHARS:
        cap.error = "the captured page has (almost) no text"
    return cap


# ------------------------------------------------------------ Gemini call
def call(client: GeminiClient | None, settings: Settings, fields: ResultFieldsConfig,
         cap: Capture) -> tuple[GeminiResult | Exception | None, list[CallRecord]]:
    """Runs in a worker thread: no database access. Returns (result | error, billed attempts)."""
    records: list[CallRecord] = []
    if cap.error:
        return None, records
    if client is None:
        return GeminiError("no Gemini client available"), records
    prompt = build_prompt(fields) + PAGE_CONTEXT.format(
        pnr=cap.pnr, surname=cap.surname or "?", passengers=", ".join(cap.passengers) or "unknown")
    try:
        res = client.generate_json(
            [prompt, "PAGE TEXT:\n" + cap.text],
            build_screen_model(fields),
            model=settings.gemini_model_result,
            prices=result_prices(settings),
            purpose="extract_result_page",
            thinking=settings.gemini_thinking_result,
            system_instruction="You copy airline booking details exactly as shown. You never guess.",
            on_call=records.append,
        )
        return res, records
    except GeminiError as exc:
        return exc, records


# ------------------------------------------------------------------ apply
def _finish(c, job_id: int, pnr: str, status: LookupStatus, note: str | None) -> None:
    c.execute("""UPDATE lookups SET status=?, note=?, parse_worker=NULL, parsed_at=?, finished_at=?, updated_at=?
                 WHERE job_id=? AND pnr=?""", (status.value, note, now(), now(), now(), job_id, pnr))
    if status != LookupStatus.PARSED:  # PARSED rows are written one by one by apply_booking
        c.execute(f"""UPDATE rows SET lookup_status=?, error_message=?, lookup_finished_at=?, updated_at=?
                      WHERE job_id=? AND COALESCE(edited_pnr, pnr)=? AND extraction_status IN {ELIGIBLE_SQL}
                      AND lookup_status='CAPTURED'""", (status.value, note, now(), now(), job_id, pnr))


def apply(db: Database, settings: Settings, fields: ResultFieldsConfig, cap: Capture,
          outcome: GeminiResult | Exception | None, records: list[CallRecord]) -> str:
    """Store the outcome of one capture. Returns the lookup status it ended in."""
    for rec in records:
        db.record_ai_call(job_id=cap.job_id, purpose=rec.purpose, model=rec.model, tokens_in=rec.tokens_in,
                          tokens_out=rec.tokens_out, cost_usd=rec.cost_usd, duration_ms=rec.duration_ms,
                          success=rec.success, error=rec.error)
    current = db.conn.execute("SELECT status, captures FROM lookups WHERE job_id=? AND pnr=?",
                              (cap.job_id, cap.pnr)).fetchone()
    if current is None or current["status"] != LookupStatus.CAPTURED or current["captures"] != cap.captures:
        log.info("Capture changed while it was read; result discarded", extra={"job_id": cap.job_id,
                                                                               "pnr": mask(cap.pnr)})
        return current["status"] if current else "GONE"
    ctx = {"job_id": cap.job_id, "pnr": mask(cap.pnr)}

    def fail(status: LookupStatus, note: str) -> str:
        with db.tx() as c:
            _finish(c, cap.job_id, cap.pnr, status, note[:300])
        log.warning("Captured page not used", extra={**ctx, "status": status.value, "reason": note[:200]})
        return status.value

    if cap.error:
        return fail(LookupStatus.PARSE_ERROR, cap.error)
    if isinstance(outcome, Exception) or outcome is None:
        return fail(LookupStatus.PARSE_ERROR, f"could not read the page: {outcome}")

    bookings = outcome.data.bookings
    booking = next((b for b in bookings if (normalise(b.pnr) or "").replace(" ", "") == cap.pnr), None)
    if booking is None:
        if bookings:
            shown = ", ".join(sorted({(normalise(b.pnr) or "?") for b in bookings}))
            return fail(LookupStatus.MISMATCH, f"the page showed booking {shown}, not {cap.pnr}")
        return fail(LookupStatus.PARSE_ERROR, "no booking details found on the captured page")
    if not booking.passengers:
        return fail(LookupStatus.PARSE_ERROR, "the page shows the booking but no passenger details")

    for pax in booking.passengers:
        if getattr(pax, "full_name", None):
            pax.full_name = split_glued_title(pax.full_name)
    candidates = [r for r in eligible_rows_by_pnr(db, cap.job_id).get(cap.pnr, [])
                  if r["lookup_status"] == LookupStatus.CAPTURED]
    filled, unmatched = apply_booking(
        db, booking, candidates, threshold=settings.confidence_threshold,
        page_text_path=cap.page_text_path, booking_keys=fields.booking_keys)
    note = f"passengers on the page not in the photo: {', '.join(unmatched)}" if unmatched else None
    with db.tx() as c:
        _finish(c, cap.job_id, cap.pnr, LookupStatus.PARSED, note)
    log.info("Captured page read", extra={**ctx, "rows_filled": filled, "unmatched": len(unmatched),
                                          "cost_usd": round(outcome.cost_usd, 6)})
    return LookupStatus.PARSED.value


# ------------------------------------------------------------- completion
def complete_if_done(db: Database, job_id: int) -> str | None:
    """A job being looked up is finished when no row is waiting (NOT_STARTED/CLAIMED/CAPTURED).
    Returns the new status, or None if it isn't finished yet."""
    job = db.get_job(job_id)
    if job is None or job["status"] not in (JobStatus.LOOKING_UP, JobStatus.READY_FOR_LOOKUP) \
            or not job["lookup_requested"]:
        return None
    counts = db.lookup_counts(job_id)
    waiting = counts[LookupStatus.NOT_STARTED] + counts[LookupStatus.CLAIMED] + counts[LookupStatus.CAPTURED]
    if counts["eligible"] == 0 or waiting:
        return None
    done = counts[LookupStatus.PARSED] + counts[LookupStatus.NOT_FOUND]
    status = JobStatus.COMPLETED if done == counts["eligible"] else JobStatus.COMPLETED_WITH_ERRORS
    if db.transition_job(job_id, [JobStatus.LOOKING_UP, JobStatus.READY_FOR_LOOKUP], status,
                         lookup_finished_at=now(), error_message=None):
        log.info("Lookups finished", extra={"job_id": job_id, "status": status.value})
        return status.value
    return None
