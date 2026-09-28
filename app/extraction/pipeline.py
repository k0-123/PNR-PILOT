"""Job-level extraction: register uploads, extract images in parallel (each image once),
write Excel #1. Used by the worker and the CLI.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.db import Database, now
from app.core.models import ExtractionStatus, GeminiExtraction, ImageStatus, JobStatus
from app.core.storage import LocalStorage, StorageError, sanitize_filename, validate_image
from app.excel.writer import write_extracted_excel

from .gemini_client import CallRecord, GeminiClient, GeminiError, MalformedResponseError, parse_structured
from .image_extractor import extract_image
from .validators import validate_row

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]  # (done, total, message)


@dataclass
class ExtractionSummary:
    job_id: int
    status: JobStatus
    images_total: int
    images_extracted: int
    images_failed: int
    rows_total: int
    rows_ready: int
    rows_review: int
    rows_duplicate: int
    cost_usd: float
    excel_key: str | None


def add_images(db: Database, storage: LocalStorage, settings: Settings, job_id: int,
               files: list[tuple[str, bytes]]) -> list[int]:
    """Validate and store uploaded images for a job. Raises StorageError on the first bad file
    (nothing from the batch is stored in that case)."""
    max_bytes = int(settings.max_file_size_mb * 1024 * 1024)
    checked = [(sanitize_filename(name), data, validate_image(name, data, max_bytes))
               for name, data in files]
    if db.count_images(job_id) + len(checked) > settings.max_images_per_job:
        raise StorageError(f"a job can have at most {settings.max_images_per_job} images")
    ids = []
    for name, data, mime in checked:
        digest = hashlib.sha256(data).hexdigest()
        key = storage.upload_key(job_id, digest, name)
        if not storage.exists(key):
            storage.write_bytes(key, data)
        image_id, _ = db.add_image(job_id, name, key, mime, digest)
        ids.append(image_id)
    return ids


def add_image_paths(db: Database, storage: LocalStorage, settings: Settings, job_id: int,
                    paths: list[Path]) -> list[int]:
    return add_images(db, storage, settings, job_id, [(p.name, p.read_bytes()) for p in paths])


class AdaptiveLimiter:
    """Caps the number of Gemini calls in flight. A 429 halves the cap (at most once every
    few seconds, never below 1), so a rate-limited key slows down instead of failing."""

    COOLDOWN_SECONDS = 5.0

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self._active = 0
        self._cond = threading.Condition()
        self._last_cut = 0.0

    def __enter__(self):
        with self._cond:
            while self._active >= self.limit:
                self._cond.wait()
            self._active += 1
        return self

    def __exit__(self, *exc):
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def rate_limited(self) -> None:
        with self._cond:
            if self.limit == 1 or time.monotonic() - self._last_cut < self.COOLDOWN_SECONDS:
                return
            self.limit = max(1, self.limit // 2)
            self._last_cut = time.monotonic()
        log.warning("Gemini rate limit hit, lowering concurrency", extra={"concurrency": self.limit})


def extract_job(db: Database, storage: LocalStorage, settings: Settings, client: GeminiClient | None,
                job_id: int, progress: ProgressFn | None = None) -> ExtractionSummary:
    """Extract every image of the job that isn't EXTRACTED yet, GEMINI_CONCURRENCY at a time.

    An image is sent to Gemini at most once: if the same file (by SHA-256) was extracted
    before, in any job, its stored response is reused. A failing image is marked
    EXTRACTION_FAILED and does not stop the job; running again retries only those.
    `client` may be None when every pending image is cached.

    Gemini calls run in threads; every database write happens here, in the calling thread.
    """
    todo = [i for i in db.get_images(job_id) if i["status"] != ImageStatus.EXTRACTED]
    db.update_job(job_id, status=JobStatus.EXTRACTING, extraction_started_at=now(), error_message=None)
    log.info("Extraction started", extra={"job_id": job_id, "images": len(todo),
                                          "concurrency": settings.gemini_concurrency})
    done = 0

    def step(message: str) -> None:
        nonlocal done
        done += 1
        db.touch_job(job_id)
        if progress:
            progress(done, len(todo), message)

    to_call = []
    for img in todo:
        if _from_cache(db, settings, job_id, img):
            step(f"Read {img['filename']} (cached)")
        else:
            to_call.append(img)

    if to_call:
        limiter = AdaptiveLimiter(settings.gemini_concurrency)
        if client is not None:
            client.on_rate_limit = limiter.rate_limited
        with ThreadPoolExecutor(max_workers=limiter.limit, thread_name_prefix="gemini") as pool:
            futures = {}
            for img in to_call:
                db.mark_image_extracting(img["id"])
                futures[pool.submit(_call_gemini, storage, settings, client, img, limiter)] = img
            for fut in as_completed(futures):
                img = futures[fut]
                _store_result(db, settings, job_id, img, *fut.result())
                step(f"Read {img['filename']}")
                if db.get_job(job_id)["status"] == JobStatus.CANCELLED:
                    log.info("Extraction cancelled", extra={"job_id": job_id})
                    for f in futures:
                        f.cancel()
                    break

    if progress:
        progress(len(todo), len(todo), "Extraction finished")
    return finish_extraction(db, storage, job_id)


def _from_cache(db, settings, job_id, img) -> bool:
    """Reuse an earlier extraction of the same image (by SHA-256). Returns True if done."""
    cached = db.find_cached_extraction(img["sha256"])
    if cached is None:
        return False
    try:
        parsed = parse_structured(cached, GeminiExtraction)
    except MalformedResponseError:
        return False  # make a fresh call instead
    rows = [validate_row(r, settings.confidence_threshold) for r in parsed.rows]
    db.save_image_extraction(job_id, img["id"], rows, raw_ai_response=cached, tokens_in=0,
                             tokens_out=0, cost_usd=0.0, attempts=0, from_cache=True)
    log.info("Image extracted from cache", extra={"job_id": job_id, "image_id": img["id"], "rows": len(rows)})
    return True


def _call_gemini(storage, settings, client, img, limiter: AdaptiveLimiter):
    """Runs in a worker thread: no database access. Returns (result | exception, call records)."""
    records: list[CallRecord] = []
    try:
        if client is None:
            raise GeminiError("no Gemini client available")
        data = storage.read_bytes(img["stored_path"])
        with limiter:
            result = extract_image(client, settings, data, img["mime_type"], on_call=records.append)
        return result, records
    except (GeminiError, OSError, StorageError) as exc:
        return exc, records


def _store_result(db, settings, job_id, img, outcome, records: list[CallRecord]) -> None:
    image_id = img["id"]
    ctx = {"job_id": job_id, "image_id": image_id}
    for rec in records:
        db.record_ai_call(job_id=job_id, image_id=image_id, purpose=rec.purpose, model=rec.model,
                          tokens_in=rec.tokens_in, tokens_out=rec.tokens_out, cost_usd=rec.cost_usd,
                          duration_ms=rec.duration_ms, success=rec.success, error=rec.error)
    if isinstance(outcome, GeminiError):
        db.mark_image_failed(image_id, str(outcome), outcome.attempts, outcome.raw_text)
        log.error("Image extraction failed", extra={**ctx, "error": str(outcome)})
        return
    if isinstance(outcome, Exception):
        db.mark_image_failed(image_id, f"cannot read stored image: {outcome}", 0)
        log.error("Image file unreadable", extra={**ctx, "error": str(outcome)})
        return

    result = outcome
    rows = [validate_row(r, settings.confidence_threshold) for r in result.data.rows]
    db.save_image_extraction(job_id, image_id, rows, raw_ai_response=result.raw_text,
                             tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                             cost_usd=result.cost_usd, attempts=result.attempts, from_cache=False)
    log.info("Image extracted", extra={
        **ctx, "rows": len(rows),
        "needs_review": sum(r.extraction_status == ExtractionStatus.NEEDS_REVIEW for r in rows),
        "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
        "cost_usd": round(result.cost_usd, 6), "duration_ms": result.duration_ms,
        "attempts": result.attempts})


def finish_extraction(db: Database, storage: LocalStorage, job_id: int) -> ExtractionSummary:
    """Write Excel #1 and set the job status from the extraction outcome."""
    images = db.get_images(job_id)
    rows = db.get_rows(job_id)
    counts = db.row_counts(job_id)
    failed = sum(i["status"] == ImageStatus.EXTRACTION_FAILED for i in images)

    excel_key = None
    if rows:
        excel_key = storage.output_key(job_id, f"job_{job_id}_extracted.xlsx")
        write_extracted_excel(rows, storage.path(excel_key))

    error = None
    if not rows:
        status = JobStatus.FAILED
        error = (f"{failed} image(s) failed extraction" if failed
                 else "no passenger rows found in the images")
    elif counts[ExtractionStatus.NEEDS_REVIEW]:
        status = JobStatus.AWAITING_REVIEW
    else:
        status = JobStatus.READY_FOR_LOOKUP
    # Only move on from EXTRACTING: a job cancelled meanwhile stays CANCELLED.
    db.transition_job(job_id, [JobStatus.EXTRACTING], status, error_message=error,
                      extraction_finished_at=now())
    status = JobStatus(db.get_job(job_id)["status"])

    return ExtractionSummary(
        job_id=job_id, status=status, images_total=len(images),
        images_extracted=sum(i["status"] == ImageStatus.EXTRACTED for i in images),
        images_failed=failed, rows_total=counts["total"],
        rows_ready=counts[ExtractionStatus.READY], rows_review=counts[ExtractionStatus.NEEDS_REVIEW],
        rows_duplicate=counts[ExtractionStatus.DUPLICATE],
        cost_usd=db.job_cost(job_id)["cost_usd"], excel_key=excel_key,
    )
