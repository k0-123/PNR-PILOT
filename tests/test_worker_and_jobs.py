"""Worker queue + job actions (review, start, pause/resume/cancel, retry)."""
from types import SimpleNamespace

import pytest

from app.core.db import Database, ago
from app.core.models import ExtractionStatus, JobStatus, LookupStatus
from app.extraction.gemini_client import GeminiClient
from app.jobs import (
    JobActionError, approve_all_valid, approve_row, cancel_job, create_job, pause_job, progress,
    reject_row, resume_job, retry_failed, start_lookups,
)
from app.worker import Worker
from tests.conftest import FakeModels, gemini_row, make_job, png

ES, LS = ExtractionStatus, LookupStatus


def smart_gemini(contents):
    """Answers image-extraction calls."""
    return {"rows": [gemini_row(), gemini_row(line_no="087", surname="PATEL", pnr="OKW002"),
                     gemini_row(line_no="088", surname="KHAN", pnr="OKW003", pnr_confidence=0.3)]}


def make_worker(settings, responder=smart_gemini):
    fake = FakeModels(responder)
    factory = lambda: GeminiClient(settings, client=SimpleNamespace(models=fake), sleep=lambda s: None)  # noqa: E731
    return Worker(settings, gemini_factory=factory), fake


# ---------------------------------------------------------------- queue

def test_claim_job_is_atomic(settings, db):
    job = db.create_job("t")
    other = Database(settings.db_path)
    first, second = db.claim_job("w1"), other.claim_job("w2")
    assert first["id"] == job and first["status"] == JobStatus.EXTRACTING
    assert second is None
    other.close()


def test_lookup_jobs_are_never_claimed_by_the_worker(db):
    """Lookups are triggered by staff in the browser extension, never by the worker."""
    job = make_job(db, [("A", "OKQ001")], status=JobStatus.READY_FOR_LOOKUP)
    start_lookups(db, job)
    assert db.claim_job("w") is None


def test_stale_extractions_are_recovered(db):
    job = db.create_job("t")
    db.claim_job("w")
    db.update_job(job, heartbeat_at=ago(600))
    assert db.recover_stale_jobs(120) == [job]
    assert db.get_job(job)["status"] == JobStatus.PENDING
    db.update_job(job, status=JobStatus.EXTRACTING, heartbeat_at=ago(5))
    assert db.recover_stale_jobs(120) == []  # fresh heartbeat -> untouched


# ------------------------------------------------------- worker end to end

def test_worker_full_flow(settings, db, storage):
    """upload -> worker extracts -> review -> job opened for lookups in the extension."""
    worker, fake = make_worker(settings)
    job = create_job(db, storage, settings, "flow", [("a.png", png(3))])
    assert db.get_job(job)["status"] == JobStatus.PENDING

    assert worker.run_once()
    assert db.get_job(job)["status"] == JobStatus.AWAITING_REVIEW
    rows = db.get_rows(job)
    assert [r["extraction_status"] for r in rows] == [ES.READY, ES.READY, ES.NEEDS_REVIEW]
    assert storage.exists(f"outputs/job_{job}/job_{job}_extracted.xlsx")

    assert approve_row(db, rows[2]["id"], surname="KHAN", first_name="", pnr="OKW003") == []
    assert db.get_job(job)["status"] == JobStatus.READY_FOR_LOOKUP
    start_lookups(db, job)
    assert not worker.run_once()  # nothing for the worker: staff do the lookups
    j = db.get_job(job)
    assert (j["status"], j["lookup_requested"]) == (JobStatus.READY_FOR_LOOKUP, 1)
    p = progress(db, job)
    assert (p.eligible, p.remaining, p.cost_usd > 0) == (3, 3, True)
    assert db.workers_online()


def test_worker_extracts_images_in_parallel(settings, db, storage):
    """GEMINI_CONCURRENCY calls run at the same time."""
    import threading
    import time
    lock, state = threading.Lock(), {"now": 0, "peak": 0, "n": 0}

    def slow(contents):
        with lock:
            state["now"] += 1
            state["n"] += 1
            n = state["n"]
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.2)
        with lock:
            state["now"] -= 1
        return {"rows": [gemini_row(pnr=f"PAR{n:03d}")]}

    settings.gemini_concurrency = 4
    worker, fake = make_worker(settings, responder=slow)
    job = create_job(db, storage, settings, "par", [(f"{i}.png", png(40 + i)) for i in range(8)])
    t0 = time.monotonic()
    worker.run_once()
    assert len(fake.calls) == 8 and state["peak"] == 4
    assert time.monotonic() - t0 < 1.2  # 8 x 0.2 s would take 1.6 s one at a time
    assert all(i["status"] == "EXTRACTED" for i in db.get_images(job))


def test_rate_limit_halves_concurrency():
    from app.extraction.pipeline import AdaptiveLimiter
    lim = AdaptiveLimiter(8)
    lim.rate_limited()
    assert lim.limit == 4
    lim.rate_limited()  # within the cooldown: unchanged
    assert lim.limit == 4
    lim._last_cut = 0
    lim.rate_limited()
    assert lim.limit == 2


def test_worker_fails_extraction_without_api_key(settings, db, storage):
    from pydantic import SecretStr
    settings.gemini_api_key = SecretStr("")
    worker = Worker(settings)
    job = create_job(db, storage, settings, "nokey", [("a.png", png(4))])
    worker.run_once()
    j = db.get_job(job)
    assert j["status"] == JobStatus.FAILED and "GEMINI_API_KEY" in j["error_message"]


# ------------------------------------------------------------- job actions

def review_job(db):
    job = make_job(db, [("SHUKLA", "OKT001"), ("PATEL", None), ("KHAN", "OKT003")],
                   status=JobStatus.AWAITING_REVIEW)
    db.conn.execute("UPDATE rows SET extraction_status='NEEDS_REVIEW' WHERE job_id=? AND seq=2", (job,))
    db.conn.commit()
    return job, db.get_rows(job)


def test_approve_rejects_invalid_values(db):
    job, rows = review_job(db)
    errors = approve_row(db, rows[1]["id"], surname="PATEL", first_name=None, pnr="AB12")
    assert errors and "PNR" in errors[0]
    assert db.get_row(rows[1]["id"])["extraction_status"] == ES.NEEDS_REVIEW
    assert approve_row(db, rows[1]["id"], surname="patel1", first_name=None, pnr="ABC123")


def test_approve_stores_only_changed_values_and_advances_job(db):
    job, rows = review_job(db)
    assert approve_row(db, rows[1]["id"], surname="PATEL", first_name="RAJ", pnr="abc123") == []
    r = db.get_row(rows[1]["id"])
    assert (r["surname"], r["edited_surname"]) == ("PATEL", None)  # unchanged -> not stored
    assert (r["pnr"], r["edited_pnr"], r["effective_pnr"]) == (None, "ABC123", "ABC123")
    assert r["extraction_status"] == ES.APPROVED and r["reviewed_at"]
    assert db.get_job(job)["status"] == JobStatus.AWAITING_REVIEW  # one row still to review
    reject_row(db, rows[2]["id"])
    assert db.get_job(job)["status"] == JobStatus.READY_FOR_LOOKUP


def test_approve_all_valid_skips_invalid(db):
    job, rows = review_job(db)
    assert approve_all_valid(db, job) == 1  # KHAN is valid, PATEL has no PNR
    assert db.get_row(rows[2]["id"])["extraction_status"] == ES.APPROVED
    assert db.get_row(rows[1]["id"])["extraction_status"] == ES.NEEDS_REVIEW


def test_start_lookups_skipping_unreviewed(db):
    job, _ = review_job(db)
    start_lookups(db, job)
    j = db.get_job(job)
    assert (j["status"], j["lookup_requested"]) == (JobStatus.READY_FOR_LOOKUP, 1)
    assert db.lookup_counts(job)["eligible"] == 1  # the 2 NEEDS_REVIEW rows are never looked up


def test_start_lookups_refused_while_running(db):
    job = make_job(db, [("A", "OKU001")], status=JobStatus.LOOKING_UP)
    with pytest.raises(JobActionError):
        start_lookups(db, job)


def test_pause_resume_cancel(db):
    job = make_job(db, [("A", "OKV001")])
    assert pause_job(db, job) and db.get_job(job)["status"] == JobStatus.PAUSED
    assert resume_job(db, job) and db.get_job(job)["status"] == JobStatus.READY_FOR_LOOKUP
    assert cancel_job(db, job) and db.get_job(job)["status"] == JobStatus.CANCELLED
    assert not resume_job(db, job) and not pause_job(db, job)


def test_retry_failed_requeues_only_failed_rows(db):
    job = make_job(db, [("A", "OKX001"), ("B", "OKX002"), ("C", "OKX003"), ("D", "OKX004")],
                   status=JobStatus.COMPLETED_WITH_ERRORS)
    ids = [r["id"] for r in db.get_rows(job)]
    for rid, st in zip(ids, [LS.PARSED, LS.NOT_FOUND, LS.MISMATCH, LS.PARSE_ERROR]):
        db.finish_row(rid, st)
    assert retry_failed(db, job) == 2
    assert [r["lookup_status"] for r in db.get_rows(job)] == [LS.PARSED, LS.NOT_FOUND,
                                                              LS.NOT_STARTED, LS.NOT_STARTED]
    j = db.get_job(job)
    assert (j["status"], j["lookup_requested"]) == (JobStatus.READY_FOR_LOOKUP, 1)


def test_cancelled_extraction_stays_cancelled(settings, db, storage):
    worker, _ = make_worker(settings)
    job = create_job(db, storage, settings, "c", [("a.png", png(5)), ("b.png", png(6))])
    cancel_job(db, job)
    assert not worker.run_once()  # cancelled jobs are never claimed
    assert db.get_job(job)["status"] == JobStatus.CANCELLED


def test_fix_and_retry_requeues_a_not_found_row(settings, db):
    from app import lookups
    from app.jobs import fix_and_retry, start_lookups

    # Row 2's PNR was misread from the photo (O instead of 0): the website says "not found".
    job = make_job(db, [("SHUKLA", "OKF001"), ("PATEL", "OKF0O2")])
    start_lookups(db, job)
    token = lookups.authenticate(db, lookups.create_token(db, "Asha"))
    lookups.claim(db, job, token, 10, settings)
    lookups.set_status(db, job, "OKF0O2", token, "NOT_FOUND")
    bad = db.get_rows(job)[1]
    assert bad["lookup_status"] == LookupStatus.NOT_FOUND

    assert fix_and_retry(db, bad["id"], surname="PATEL", first_name=None, pnr="okf002x") != []  # invalid PNR
    assert fix_and_retry(db, db.get_rows(job)[0]["id"], surname="SHUKLA", first_name=None, pnr="OKF001") != []

    assert fix_and_retry(db, bad["id"], surname="patel", first_name=None, pnr="okf002") == []
    row = db.get_row(bad["id"])
    assert (row["edited_pnr"], row["lookup_status"], row["error_message"]) == ("OKF002", "NOT_STARTED", None)
    old = db.conn.execute("SELECT status FROM lookups WHERE job_id=? AND pnr='OKF0O2'", (job,)).fetchone()
    assert old["status"] == LookupStatus.NOT_FOUND  # the old PNR stays done
    leased = [x["pnr"] for x in lookups.claim(db, job, token, 10, settings)]
    assert "OKF002" in leased and "OKF0O2" not in leased  # the corrected PNR is searched again
