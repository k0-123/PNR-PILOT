"""FLT credits: 1 FLT per row that got its result, held while a job is open, never below zero."""
import pytest

from app import credits, lookups
from app.core.config import load_result_fields
from app.core.models import JobStatus, LookupStatus
from app.jobs import JobActionError, fix_and_retry, start_lookups
from app.lookup.gds_screens import apply_booking, build_screen_model
from tests.conftest import make_job


def rows_of(db, job):
    return db.get_rows(job)


def result_for(db, row):
    """Give a row its result the way the worker does (website page / GDS screen)."""
    model = build_screen_model(load_result_fields())
    booking = model.model_validate({"bookings": [{"pnr": row["effective_pnr"], "pnr_confidence": 1.0, "passengers": [
        {"surname": row["effective_surname"], "first_name": None, "full_name": "X", "confidence": 1.0}]}]})
    apply_booking(db, booking.bookings[0], [row], threshold=0.85)


def test_inactive_until_first_plan(db):
    job = make_job(db, [("A", "OKC001")], status=JobStatus.AWAITING_REVIEW)
    assert not credits.is_active(db)
    start_lookups(db, job)  # no plan yet: nothing is limited
    result_for(db, rows_of(db, job)[0])
    b = credits.balance(db, None)
    assert (b.active, b.balance, b.used, b.warning) == (False, 0, 0, None)


def test_plan_hold_charge_and_release(db):
    credits.add_plan(db, 10, None, plan="Plan 10k-test")
    job = make_job(db, [("A", "OKC101"), ("B", "OKC102"), ("C", "OKC103"), ("D", "OKC104"), ("E", "OKC105")],
                   status=JobStatus.AWAITING_REVIEW)
    start_lookups(db, job)
    b = credits.balance(db, None)
    assert (b.balance, b.held, b.available) == (10, 5, 5)  # the open job's rows are on hold

    other = make_job(db, [(s, f"OKD10{i}") for i, s in enumerate("FGHIJK")], status=JobStatus.AWAITING_REVIEW)
    with pytest.raises(JobActionError, match="Needs 6 FLT, you have 5"):
        start_lookups(db, other)
    assert db.get_job(other)["status"] == JobStatus.AWAITING_REVIEW

    rows = rows_of(db, job)
    for r in rows[:3]:
        result_for(db, r)                        # 3 results: 3 FLT used
    result_for(db, db.get_row(rows[0]["id"]))    # a re-read of the same row is free
    for r in rows[3:]:
        db.finish_row(r["id"], LookupStatus.NOT_FOUND)  # not found: free, hold released
    b = credits.balance(db, None)
    assert (b.balance, b.used, b.held, b.available) == (7, 3, 0, 7)
    start_lookups(db, other)                     # now 6 <= 7 fits


def test_fix_and_retry_needs_one_flt(db):
    credits.add_plan(db, 1, None)
    job = make_job(db, [("A", "OKE001"), ("B", "OKE0O2")], status=JobStatus.AWAITING_REVIEW)
    rows = rows_of(db, job)
    db.finish_row(rows[1]["id"], LookupStatus.NOT_FOUND)
    result_for(db, rows[0])                      # the only FLT is used
    assert credits.balance(db, None).available == 0
    errors = fix_and_retry(db, rows[1]["id"], surname="B", first_name=None, pnr="OKE002")
    assert errors and "Needs 1 FLT" in errors[0]
    credits.add_plan(db, 5, None)
    assert fix_and_retry(db, rows[1]["id"], surname="B", first_name=None, pnr="OKE002") == []


def test_extension_claims_stop_when_balance_cannot_cover_open_jobs(db, settings):
    job = make_job(db, [("A", "OKF001"), ("B", "OKF002"), ("C", "OKF003")])
    start_lookups(db, job)                       # opened before FLT was switched on
    credits.add_plan(db, 2, None)                      # 2 FLT for 3 open rows
    token = lookups.authenticate(db, lookups.create_token(db, "Asha"))
    with pytest.raises(lookups.LookupRequestError, match="FLT") as exc:
        lookups.claim(db, job, token, 10, settings)
    assert exc.value.code == 402
    credits.add_plan(db, 1, None)
    assert len(lookups.claim(db, job, token, 10, settings)) == 3


def test_low_balance_warning_and_ledger(db):
    credits.add_plan(db, 1000, None, plan="Plan 1k", by_user="me@x.com")
    job = make_job(db, [("A", "OKG001")])
    result_for(db, rows_of(db, job)[0])
    b = credits.balance(db, None)
    assert b.warning is None
    db.conn.execute("INSERT INTO flt_ledger(created_at, kind, amount, note) VALUES ('2026-09-25', 'adjust', -800, 't')")
    db.conn.commit()
    assert "low" in credits.balance(db, None).warning  # 199 left of a 1,000 plan (< 20%)
    kinds = [(r["kind"], r["amount"]) for r in credits.ledger(db)]
    assert ("topup", 1000) in kinds and ("charge", -1) in kinds


def test_add_plan_rejects_non_positive(db):
    with pytest.raises(ValueError):
        credits.add_plan(db, 0, None)


def test_is_admin_matches_the_configured_email_only(settings):
    s = settings.model_copy(update={"flt_admin_email": "Shekhawatk271@gmail.com"})
    assert credits.is_admin(s, " shekhawatk271@GMAIL.com ")
    assert not credits.is_admin(s, "someone@example.com")
    assert not credits.is_admin(s, None)
    assert not credits.is_admin(settings.model_copy(update={"flt_admin_email": ""}), "")
