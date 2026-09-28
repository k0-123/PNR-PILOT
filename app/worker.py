"""Background worker: picks jobs from SQLite and does the heavy work (realplan sections 6, 8, 12).

    python -m app.worker

- Claims jobs atomically (two workers never take the same job).
- PENDING -> photo extraction (parallel Gemini calls); queued GDS PNR screens.
- Website pages captured by the browser extension -> result columns (parallel Gemini calls).
  There are no automated website lookups: staff trigger every search in the extension.
- Finishes jobs whose lookups are all done; rebuilds the final Excel while lookups run
  (at most every LIVE_EXCEL_SECONDS, and once when a job finishes).
- Heartbeats every few seconds; extractions of a worker that died are re-queued automatically.
- Hourly cleanup of files older than DELETE_FILES_AFTER_DAYS.
Keeps running when the browser tab is closed: the UI only reads/writes the database.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.core.config import Settings, get_settings, load_result_fields
from app.core.db import Database, ago
from app.core.logging_setup import setup_logging
from app.core.models import JobStatus
from app.core.storage import LocalStorage
from app.extraction.gemini_client import GeminiClient, GeminiError
from app.extraction.pipeline import extract_job
from app.jobs import export_final
from app.lookup import page_results
from app.lookup.gds_screens import process_screen

log = logging.getLogger("app.worker")

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 5.0
STALE_SECONDS = 120.0
CLEANUP_EVERY_SECONDS = 3600.0
LIVE_EXCEL_SECONDS = 30.0
LIVE_STATUSES = ("LOOKING_UP", "PAUSED", "READY_FOR_LOOKUP", "COMPLETED", "COMPLETED_WITH_ERRORS")


class Worker:
    def __init__(self, settings: Settings, *, gemini_factory: Callable[[], GeminiClient] | None = None):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.storage = LocalStorage(settings.data_dir)
        self.id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.gemini_factory = gemini_factory or (lambda: GeminiClient(settings))
        self.current_job: int | None = None
        self.stopping = threading.Event()
        self._last_cleanup = 0.0
        self._exported: dict[int, tuple[str, float]] = {}  # job -> (data fingerprint, time)

    # --------------------------------------------------------------- loop
    def run_forever(self) -> None:
        log.info("Worker started", extra={"worker_id": self.id})
        if n := self.db.recover_stale_screens():
            log.warning("Re-queued PNR screens from a stopped worker", extra={"screens": n})
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat")
        hb.start()
        while not self.stopping.is_set():
            try:
                if not self.run_once():
                    self.stopping.wait(POLL_SECONDS)
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("Worker loop error")
                self.stopping.wait(POLL_SECONDS)
        log.info("Worker stopped", extra={"worker_id": self.id})

    def stop(self) -> None:
        self.stopping.set()

    def run_once(self) -> bool:
        """Housekeeping + at most one job. Returns True if a job was processed."""
        self.db.worker_heartbeat(self.id, os.getpid(), socket.gethostname())
        self._maybe_cleanup()
        for job_id in self.db.recover_stale_jobs(STALE_SECONDS):
            log.warning("Re-queued job from a stopped worker", extra={"job_id": job_id})
        self._finish_and_export()
        job = self.db.claim_job(self.id)
        if job is None:
            return self._screen() or self._parse_captures()
        self.current_job = job["id"]
        try:
            self._extract(job["id"])
        finally:
            self.current_job = None
        return True

    # --------------------------------------------------------------- jobs
    def _extract(self, job_id: int) -> None:
        try:
            client = self.gemini_factory()
        except GeminiError as exc:
            self.db.transition_job(job_id, [JobStatus.EXTRACTING], JobStatus.FAILED, error_message=str(exc))
            return
        extract_job(self.db, self.storage, self.settings, client, job_id)

    def _screen(self) -> bool:
        """Read one queued GDS PNR screenshot. Returns True if one was processed."""
        screen = self.db.claim_pnr_screen()
        if screen is None:
            return False
        try:
            client = self.gemini_factory()
        except GeminiError as exc:
            self.db.finish_pnr_screen(screen["id"], status="FAILED", error=str(exc))
            return True
        job_before = self.db.get_job(screen["job_id"])["status"]
        fields = load_result_fields()
        process_screen(self.db, self.storage, self.settings, client, fields, screen)
        job = self.db.get_job(screen["job_id"])
        if job["status"] == JobStatus.COMPLETED or job_before == JobStatus.COMPLETED:
            export_final(self.db, self.storage, job["id"], fields)
        return True

    # ---------------------------------------------------- captured pages
    def _parse_captures(self) -> bool:
        """Read a batch of pages captured by the extension. Returns True if any were processed."""
        batch = page_results.claim(self.db, self.id, self.settings.gemini_concurrency * 2)
        if not batch:
            return False
        try:
            client = self.gemini_factory()
        except GeminiError as exc:
            client = None
            log.error("Can't read captured pages", extra={"error": str(exc)})
        fields = load_result_fields()
        batch = [page_results.prepare(self.db, self.storage, cap) for cap in batch]
        with ThreadPoolExecutor(max_workers=self.settings.gemini_concurrency, thread_name_prefix="pages") as pool:
            futures = {pool.submit(page_results.call, client, self.settings, fields, cap): cap for cap in batch}
            for fut in as_completed(futures):
                page_results.apply(self.db, self.settings, fields, futures[fut], *fut.result())
        for job_id in {cap.job_id for cap in batch}:
            self.db.touch_job(job_id)
        return True

    def _finish_and_export(self) -> None:
        """Complete finished jobs; keep each active job's final Excel fresh (debounced)."""
        jobs = self.db.conn.execute(
            f"""SELECT j.id, j.status, (SELECT COUNT(*) || '|' || COALESCE(MAX(updated_at), '')
                                        FROM rows WHERE job_id=j.id) || '|' || j.status AS fp
                FROM jobs j WHERE j.status IN ({','.join('?' * len(LIVE_STATUSES))})
                AND j.lookup_requested=1 AND j.updated_at >= ?""",
            (*LIVE_STATUSES, ago(86400))).fetchall()
        for j in jobs:
            finished = page_results.complete_if_done(self.db, j["id"])
            # fingerprint = rows count/last change + job status (same as the next check computes)
            fp = j["fp"] if not finished else f"{j['fp'].rsplit('|', 1)[0]}|{finished}"
            last_fp, last_t = self._exported.get(j["id"], ("", 0.0))
            if fp == last_fp or (not finished and time.monotonic() - last_t < LIVE_EXCEL_SECONDS):
                continue
            try:
                export_final(self.db, self.storage, j["id"], load_result_fields())
                self._exported[j["id"]] = (fp, time.monotonic())
            except Exception:  # noqa: BLE001 - a broken export must not stop the worker
                log.exception("Final Excel export failed", extra={"job_id": j["id"]})

    # -------------------------------------------------------- background
    def _heartbeat_loop(self) -> None:
        db = Database(self.settings.db_path)
        try:
            while not self.stopping.wait(HEARTBEAT_SECONDS):
                try:
                    db.worker_heartbeat(self.id, os.getpid(), socket.gethostname())
                    if self.current_job is not None:
                        db.touch_job(self.current_job)
                except Exception:  # noqa: BLE001
                    log.exception("Heartbeat failed")
        finally:
            db.close()

    def _maybe_cleanup(self) -> None:
        if time.monotonic() - self._last_cleanup < CLEANUP_EVERY_SECONDS and self._last_cleanup:
            return
        self._last_cleanup = time.monotonic()
        n = self.storage.delete_older_than(self.settings.delete_files_after_days)
        if n:
            log.info("Cleanup deleted old files", extra={"files": n,
                                                         "days": self.settings.delete_files_after_days})


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    worker = Worker(settings)

    def _signal(signum, _frame):
        log.info("Stop signal received", extra={"signal": signum})
        worker.stop()

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)
    worker.run_forever()


if __name__ == "__main__":
    main()
