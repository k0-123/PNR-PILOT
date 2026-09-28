"""Job actions shared by the UI, the worker and the CLI.

The UI only calls these (they read/write SQLite and storage); heavy work happens in the worker.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from app import credits
from app.core.config import ResultFieldsConfig, Settings, load_result_fields
from app.core.db import Database, now
from app.core.models import LOOKUP_RETRYABLE, ExtractionStatus, JobStatus, LookupStatus
from app.core.storage import LocalStorage, sanitize_filename, validate_image
from app.excel.writer import final_table, table_to_csv_bytes, write_final_excel
from app.extraction.pipeline import add_images
from app.extraction.validators import normalise, validate_edit

log = logging.getLogger(__name__)

ACTIVE = {JobStatus.PENDING, JobStatus.EXTRACTING, JobStatus.LOOKING_UP}
CANCELLABLE = [JobStatus.PENDING, JobStatus.EXTRACTING, JobStatus.AWAITING_REVIEW,
               JobStatus.READY_FOR_LOOKUP, JobStatus.LOOKING_UP, JobStatus.PAUSED]
LOOKUP_STARTABLE = [JobStatus.AWAITING_REVIEW, JobStatus.READY_FOR_LOOKUP, JobStatus.COMPLETED,
                    JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED]


class JobActionError(Exception):
    pass


def _ensure_flt(db: Database, job_id: int, extra: int = 0) -> None:
    """Enough FLT credits for the job's open rows (+ `extra` rows about to be re-opened)?"""
    try:
        credits.ensure_affordable(db, job_id, extra)
    except credits.NotEnoughFLT as exc:
        raise JobActionError(str(exc)) from None


def _uncharged(db: Database, job_id: int, statuses) -> int:
    """Eligible rows of the job in `statuses` that were never charged (re-opening them may cost FLT)."""
    sts = [str(s) for s in statuses]
    return db.conn.execute(
        f"""SELECT COUNT(*) FROM rows WHERE job_id=? AND flt_charged_at IS NULL
            AND extraction_status IN ('READY','APPROVED') AND lookup_status IN ({','.join('?' * len(sts))})""",
        (job_id, *sts)).fetchone()[0]


# --------------------------------------------------------------- creating
def create_job(db: Database, storage: LocalStorage, settings: Settings, name: str,
               files: list[tuple[str, bytes]]) -> int:
    """Store uploads and queue the job for extraction (status PENDING)."""
    if not files:
        raise JobActionError("no images uploaded")
    job_id = db.create_job(name)
    try:
        add_images(db, storage, settings, job_id, files)
    except Exception:
        db.update_job(job_id, status=JobStatus.FAILED, error_message="upload rejected")
        raise
    log.info("Job created", extra={"job_id": job_id, "images": len(files)})
    return job_id


def add_pnr_screens(db: Database, storage: LocalStorage, settings: Settings, job_id: int,
                    files: list[tuple[str, bytes]]) -> list[int]:
    """Queue GDS PNR display screenshots for the worker (booking details without a website)."""
    if db.get_job(job_id) is None:
        raise JobActionError("job not found")
    if not files:
        raise JobActionError("no images uploaded")
    # Screens can fill any eligible row of the job, also finished ones: all uncharged rows may cost FLT.
    _ensure_flt(db, job_id, extra=_uncharged(db, job_id, [s for s in LookupStatus if s not in (
        LookupStatus.NOT_STARTED, LookupStatus.CLAIMED, LookupStatus.CAPTURED)]))
    max_bytes = int(settings.max_file_size_mb * 1024 * 1024)
    checked = [(sanitize_filename(n), d, validate_image(n, d, max_bytes)) for n, d in files]
    ids = []
    for name, data, mime in checked:
        digest = hashlib.sha256(data).hexdigest()
        key = f"screens/job_{int(job_id)}/{digest[:12]}_{name}"
        if not storage.exists(key):
            storage.write_bytes(key, data)
        ids.append(db.add_pnr_screen(job_id, name, key, mime, digest))
    log.info("PNR screens queued", extra={"job_id": job_id, "screens": len(ids)})
    return ids


# ---------------------------------------------------------------- review
def _refresh_review_state(db: Database, job_id: int) -> None:
    if db.row_counts(job_id)[ExtractionStatus.NEEDS_REVIEW] == 0:
        db.transition_job(job_id, [JobStatus.AWAITING_REVIEW], JobStatus.READY_FOR_LOOKUP)


def approve_row(db: Database, row_id: int, *, surname: str | None, first_name: str | None,
                pnr: str | None) -> list[str]:
    """Approve a reviewed row after re-validating it. Returns validation errors (empty = saved).
    Edited values are stored next to the original AI values, only where they differ."""
    row = db.get_row(row_id)
    if row is None:
        return ["row not found"]
    surname, pnr, errors = validate_edit(surname, pnr)
    if errors:
        return errors
    first_name = normalise(first_name)
    db.review_row(
        row_id,
        surname=surname if surname != row["surname"] else None,
        first_name=first_name if first_name != row["first_name"] else None,
        pnr=pnr if pnr != row["pnr"] else None,
        status=ExtractionStatus.APPROVED,
    )
    _refresh_review_state(db, row["job_id"])
    return []


def reject_row(db: Database, row_id: int) -> None:
    row = db.get_row(row_id)
    db.review_row(row_id, surname=row["edited_surname"], first_name=row["edited_first_name"],
                  pnr=row["edited_pnr"], status=ExtractionStatus.REJECTED)
    _refresh_review_state(db, row["job_id"])


def approve_all_valid(db: Database, job_id: int) -> int:
    """Approve every NEEDS_REVIEW row whose surname and PNR pass validation as they are."""
    n = 0
    for r in db.get_rows(job_id):
        if r["extraction_status"] == ExtractionStatus.NEEDS_REVIEW:
            if not approve_row(db, r["id"], surname=r["effective_surname"],
                               first_name=r["effective_first_name"], pnr=r["effective_pnr"]):
                n += 1
    return n


# --------------------------------------------------------------- lookups
def start_lookups(db: Database, job_id: int) -> None:
    """Open the job for lookups in the browser extension. Rows still NEEDS_REVIEW are skipped
    (never looked up)."""
    job = db.get_job(job_id)
    if job["status"] not in LOOKUP_STARTABLE:
        raise JobActionError(f"lookups can't start while the job is {job['status']}")
    _ensure_flt(db, job_id)
    if not db.transition_job(job_id, LOOKUP_STARTABLE, JobStatus.READY_FOR_LOOKUP,
                             lookup_requested=1, error_message=None):
        raise JobActionError("job changed meanwhile, try again")


def pause_job(db: Database, job_id: int) -> bool:
    return db.transition_job(job_id, [JobStatus.LOOKING_UP, JobStatus.READY_FOR_LOOKUP], JobStatus.PAUSED,
                             error_message="paused by user")


def resume_job(db: Database, job_id: int) -> bool:
    """Blocked rows are retried on resume."""
    job = db.get_job(job_id)
    if job["status"] != JobStatus.PAUSED:
        return False
    db.requeue_rows(job_id, [LookupStatus.BLOCKED])
    return db.transition_job(job_id, [JobStatus.PAUSED], JobStatus.READY_FOR_LOOKUP,
                             lookup_requested=1, error_message=None)


def cancel_job(db: Database, job_id: int) -> bool:
    return db.transition_job(job_id, CANCELLABLE, JobStatus.CANCELLED, lookup_requested=0,
                             error_message="cancelled by user")


def retry_failed(db: Database, job_id: int) -> int:
    """Re-queue only rows that finished without a result (MISMATCH/PARSE_ERROR/SKIPPED/BLOCKED).
    A PARSE_ERROR whose page text was saved is read again by the worker, without a new search;
    everything else goes back to the extension's queue. Returns the number of rows re-queued."""
    _ensure_flt(db, job_id, extra=_uncharged(db, job_id, LOOKUP_RETRYABLE))
    before = db.lookup_counts(job_id)[LookupStatus.CAPTURED]
    db.requeue_parse(job_id, [LookupStatus.PARSE_ERROR])
    n = db.lookup_counts(job_id)[LookupStatus.CAPTURED] - before
    n += db.requeue_rows(job_id, LOOKUP_RETRYABLE)
    if n:
        db.transition_job(job_id, LOOKUP_STARTABLE + [JobStatus.PAUSED], JobStatus.READY_FOR_LOOKUP,
                          lookup_requested=1, error_message=None)
    return n


FIXABLE = frozenset({LookupStatus.NOT_FOUND, LookupStatus.MISMATCH, LookupStatus.SKIPPED})


def fix_and_retry(db: Database, row_id: int, *, surname: str | None, first_name: str | None,
                  pnr: str | None) -> list[str]:
    """Correct a row the website couldn't find (e.g. a PNR misread from the photo) and send it
    back to the extension's queue. Edited values are stored next to the AI values, like a review.
    Returns validation errors (empty = queued)."""
    row = db.get_row(row_id)
    if row is None:
        return ["row not found"]
    if row["extraction_status"] not in (ExtractionStatus.READY, ExtractionStatus.APPROVED) \
            or row["lookup_status"] not in FIXABLE:
        return ["only rows that were not found, mismatched or skipped can be fixed here"]
    surname, pnr, errors = validate_edit(surname, pnr)
    if errors:
        return errors
    try:
        credits.ensure_affordable(db, row["job_id"], extra=0 if row["flt_charged_at"] else 1)
    except credits.NotEnoughFLT as exc:
        return [str(exc)]
    first_name = normalise(first_name)
    db.review_row(
        row_id,
        surname=surname if surname != row["surname"] else None,
        first_name=first_name if first_name != row["first_name"] else None,
        pnr=pnr if pnr != row["pnr"] else None,
        status=ExtractionStatus.APPROVED,
    )
    with db.tx() as c:
        c.execute("UPDATE rows SET lookup_status='NOT_STARTED', error_message=NULL, attempts=0, updated_at=? "
                  "WHERE id=?", (now(), row_id))
        # The (new) PNR must be searchable again, also if it was finished before.
        c.execute("""UPDATE lookups SET status='NOT_STARTED', token_id=NULL, lease_until=NULL, note=NULL,
                         updated_at=? WHERE job_id=? AND pnr=? AND status NOT IN ('NOT_STARTED','CLAIMED')""",
                  (now(), row["job_id"], pnr))
    db.transition_job(row["job_id"], LOOKUP_STARTABLE + [JobStatus.PAUSED], JobStatus.READY_FOR_LOOKUP,
                      lookup_requested=1, error_message=None)
    log.info("Row fixed and re-queued", extra={"job_id": row["job_id"], "row_id": row_id})
    return []


def reparse_all(db: Database, job_id: int) -> int:
    """Read every saved website page of the job again (e.g. after changing result_fields.yaml).
    No website search is repeated. Returns the number of PNRs queued for reading."""
    n = db.requeue_parse(job_id, [LookupStatus.PARSED, LookupStatus.PARSE_ERROR])
    if n:
        db.transition_job(job_id, LOOKUP_STARTABLE + [JobStatus.PAUSED, JobStatus.LOOKING_UP],
                          JobStatus.LOOKING_UP, lookup_requested=1, error_message=None)
    return n


# -------------------------------------------------------------- progress
@dataclass
class Progress:
    images_total: int
    images_done: int
    images_failed: int
    rows_total: int
    review: int
    duplicates: int
    rejected: int
    eligible: int
    lookup_done: int
    success: int
    not_found: int
    failed: int
    in_progress: int
    remaining: int
    cost_usd: float

    @property
    def lookup_pct(self) -> float:
        return (self.lookup_done / self.eligible) if self.eligible else 0.0

    @property
    def extraction_pct(self) -> float:
        return ((self.images_done + self.images_failed) / self.images_total) if self.images_total else 0.0


def progress(db: Database, job_id: int) -> Progress:
    images = db.get_images(job_id)
    rc = db.row_counts(job_id)
    lc = db.lookup_counts(job_id)
    success, not_found = lc[LookupStatus.PARSED], lc[LookupStatus.NOT_FOUND]
    failed = sum(lc[s] for s in LOOKUP_RETRYABLE)
    in_progress = lc[LookupStatus.CLAIMED] + lc[LookupStatus.CAPTURED]
    return Progress(
        images_total=len(images),
        images_done=sum(i["status"] == "EXTRACTED" for i in images),
        images_failed=sum(i["status"] == "EXTRACTION_FAILED" for i in images),
        rows_total=rc["total"], review=rc[ExtractionStatus.NEEDS_REVIEW],
        duplicates=rc[ExtractionStatus.DUPLICATE], rejected=rc[ExtractionStatus.REJECTED],
        eligible=lc["eligible"], lookup_done=success + not_found + failed, success=success,
        not_found=not_found, failed=failed, in_progress=in_progress,
        remaining=lc[LookupStatus.NOT_STARTED] + in_progress,
        cost_usd=db.job_cost(job_id)["cost_usd"],
    )


# ---------------------------------------------------------------- export
def _duration(job) -> str:
    try:
        start = datetime.fromisoformat(job["created_at"])
        end = datetime.fromisoformat(job["lookup_finished_at"] or job["extraction_finished_at"] or now())
    except (TypeError, ValueError):
        return ""
    secs = int((end - start).total_seconds())
    return f"{secs // 3600}h {secs % 3600 // 60}m {secs % 60}s"


def summary_pairs(db: Database, job_id: int) -> list[tuple[str, object]]:
    p = progress(db, job_id)
    job = db.get_job(job_id)
    return [
        ("Job", f"#{job_id} {job['name']}"),
        ("Status", job["status"]),
        ("Total rows", p.rows_total),
        ("Success", p.success),
        ("Not found", p.not_found),
        ("Failed", p.failed),
        ("Needs review", p.review),
        ("Duplicates", p.duplicates),
        ("Rejected", p.rejected),
        ("Total Gemini cost (USD)", round(p.cost_usd, 4)),
        ("Job duration", _duration(job)),
    ]


def build_final(db: Database, job_id: int, fields: ResultFieldsConfig | None = None):
    fields = fields or load_result_fields()
    return final_table(db.get_rows(job_id), fields)


def export_final(db: Database, storage: LocalStorage, job_id: int,
                 fields: ResultFieldsConfig | None = None) -> tuple[str, str]:
    """Write job_<id>_final.xlsx and job_<id>_final.csv. Returns their storage keys."""
    headers, rows = build_final(db, job_id, fields)
    xlsx_key = storage.output_key(job_id, f"job_{job_id}_final.xlsx")
    csv_key = storage.output_key(job_id, f"job_{job_id}_final.csv")
    write_final_excel(storage.path(xlsx_key), headers, rows, summary_pairs(db, job_id))
    storage.write_bytes(csv_key, table_to_csv_bytes(headers, rows))
    return xlsx_key, csv_key
