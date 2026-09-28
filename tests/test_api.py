"""Extension API (app/api.py + app/lookups.py): auth, leases, captures, statuses, progress, profiles."""
import base64

import pytest
from fastapi.testclient import TestClient

from app import lookups
from app.api import create_app
from app.core.db import ago
from app.core.models import ExtractionStatus, JobStatus, LookupStatus
from app.jobs import resume_job, retry_failed, start_lookups
from tests.conftest import make_job, png

LS = LookupStatus


@pytest.fixture
def client(settings, db):
    return TestClient(create_app(settings))


@pytest.fixture
def token(db):
    return lookups.create_token(db, "Asha")


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def open_job(db, rows, **kw):
    job = make_job(db, rows, **kw)
    start_lookups(db, job)
    return job


def make_family(db, job):
    """Row 2 becomes a second passenger (other first name) on row 1's booking."""
    db.conn.execute("UPDATE rows SET first_name='RAJ', extraction_status='READY', duplicate_of=NULL "
                    "WHERE job_id=? AND seq=1", (job,))
    db.conn.commit()


def statuses(db, job):
    return [r["lookup_status"] for r in db.get_rows(job)]


# ------------------------------------------------------------------ auth

def test_auth_required_and_revocation(client, db, token):
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers=hdr("gds_wrong")).status_code == 401
    assert client.get("/api/me", headers=hdr(token)).json() == {"name": "Asha"}
    tid = lookups.list_tokens(db)[0]["id"]
    assert lookups.revoke_token(db, tid)
    assert client.get("/api/me", headers=hdr(token)).status_code == 401


def test_token_is_stored_hashed(db, token):
    stored = db.conn.execute("SELECT token_hash FROM api_tokens").fetchone()[0]
    assert token not in stored and stored == lookups.hash_token(token)


# ---------------------------------------------------------------- claims

def test_jobs_lists_only_opened_jobs(client, db, token):
    open_job(db, [("A", "OKA001")])
    make_job(db, [("B", "OKA002")])  # READY_FOR_LOOKUP but never opened
    names = [j["id"] for j in client.get("/api/jobs", headers=hdr(token)).json()]
    assert len(names) == 1


def test_claim_leases_unique_pnrs_once_across_two_staff(client, db, token):
    # a family: 2 rows, 1 PNR -> one lookup
    job = open_job(db, [("SHUKLA", "FAM001"), ("SHUKLA", "FAM001"), ("B", "OKB002"), ("C", "OKB003")])
    make_family(db, job)
    other = lookups.create_token(db, "Ben")
    a = client.post(f"/api/jobs/{job}/claim?n=2", headers=hdr(token)).json()["leases"]
    b = client.post(f"/api/jobs/{job}/claim?n=5", headers=hdr(other)).json()["leases"]
    assert [x["pnr"] for x in a] == ["FAM001", "OKB002"]
    assert [x["pnr"] for x in b] == ["OKB003"]
    assert [p["first_name"] for p in a[0]["passengers"]] == ["SIDDHESHVA", "RAJ"]
    assert a[0]["surname"] == "SHUKLA"
    assert db.get_job(job)["status"] == JobStatus.LOOKING_UP
    assert statuses(db, job) == [LS.CLAIMED] * 4
    # claiming again renews and returns the same leases (no new ones: nothing free)
    again = client.post(f"/api/jobs/{job}/claim?n=2", headers=hdr(token)).json()["leases"]
    assert [x["pnr"] for x in again] == ["FAM001", "OKB002"]


def test_expired_lease_can_be_taken_by_someone_else(client, db, token):
    job = open_job(db, [("A", "OKC001")])
    client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    other = lookups.create_token(db, "Ben")
    assert client.post(f"/api/jobs/{job}/claim", headers=hdr(other)).json()["leases"] == []
    db.conn.execute("UPDATE lookups SET lease_until=?", (ago(60),))
    db.conn.commit()
    got = client.post(f"/api/jobs/{job}/claim", headers=hdr(other)).json()["leases"]
    assert [x["pnr"] for x in got] == ["OKC001"]


def test_rows_needing_review_are_never_claimed(client, db, token):
    job = make_job(db, [("A", "OKD001"), ("B", "OKD002")], status=JobStatus.AWAITING_REVIEW)
    db.conn.execute("UPDATE rows SET extraction_status='NEEDS_REVIEW' WHERE job_id=? AND seq=1", (job,))
    db.conn.commit()
    start_lookups(db, job)  # "skip 1 unreviewed"
    got = client.post(f"/api/jobs/{job}/claim", headers=hdr(token)).json()["leases"]
    assert [x["pnr"] for x in got] == ["OKD001"]


def test_claim_refused_for_job_not_opened(client, db, token):
    job = make_job(db, [("A", "OKE001")])
    r = client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    assert r.status_code == 409 and "open it for lookups" in r.json()["detail"]


def test_release_returns_leases(client, db, token):
    job = open_job(db, [("A", "OKF001"), ("B", "OKF002")])
    client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    assert client.post(f"/api/jobs/{job}/release", headers=hdr(token), json={"pnrs": ["okf002"]}).json() == \
        {"released": 1}
    assert statuses(db, job) == [LS.CLAIMED, LS.NOT_STARTED]
    assert client.post(f"/api/jobs/{job}/release", headers=hdr(token)).json() == {"released": 1}
    assert statuses(db, job) == [LS.NOT_STARTED] * 2


# --------------------------------------------------------------- capture

def test_capture_is_stored_and_idempotent(client, db, storage, token):
    job = open_job(db, [("SHUKLA", "OKG001"), ("SHUKLA", "OKG001")])
    make_family(db, job)
    client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    shot = base64.b64encode(png(9)).decode()
    url = f"/api/jobs/{job}/lookups/OKG001/capture"
    r = client.post(url, headers=hdr(token), json={"text": "Booking OKG001 CONFIRMED", "screenshot_b64": shot,
                                                   "url": "https://x/search"})
    assert r.json() == {"status": "CAPTURED", "stored": True}
    lk = db.conn.execute("SELECT * FROM lookups WHERE job_id=?", (job,)).fetchone()
    assert storage.read_bytes(lk["page_text_path"]) == b"Booking OKG001 CONFIRMED"
    assert storage.read_bytes(lk["screenshot_path"]) == png(9)
    assert lk["looked_up_by"] == "Asha" and lk["lease_until"] is None
    assert statuses(db, job) == [LS.CAPTURED] * 2  # both family rows

    # the outbox may send it again: ignored, first capture kept
    r2 = client.post(url, headers=hdr(token), json={"text": "something else"})
    assert r2.json() == {"status": "CAPTURED", "stored": False}
    assert storage.read_bytes(lk["page_text_path"]) == b"Booking OKG001 CONFIRMED"
    # Re-capture (Alt+R) replaces it
    r3 = client.post(url, headers=hdr(token), json={"text": "Booking OKG001 v2", "recapture": True})
    assert r3.json()["stored"] is True
    assert storage.read_bytes(lk["page_text_path"]) == b"Booking OKG001 v2"


def test_capture_validation(client, db, token):
    job = open_job(db, [("A", "OKH001")])
    url = f"/api/jobs/{job}/lookups/OKH001/capture"
    assert client.post(url, headers=hdr(token), json={}).status_code == 400
    assert client.post(url, headers=hdr(token), json={"screenshot_b64": "@@@"}).status_code == 400
    bad_img = base64.b64encode(b"GIF89a....").decode()
    assert client.post(url, headers=hdr(token), json={"screenshot_b64": bad_img}).status_code == 400
    assert client.post(f"/api/jobs/{job}/lookups/NOTME1/capture", headers=hdr(token),
                       json={"text": "x"}).status_code == 404


def test_capture_refused_while_someone_else_holds_the_lease(client, db, token):
    job = open_job(db, [("A", "OKI001")])
    client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    other = lookups.create_token(db, "Ben")
    r = client.post(f"/api/jobs/{job}/lookups/OKI001/capture", headers=hdr(other), json={"text": "x"})
    assert r.status_code == 409


# ---------------------------------------------------------------- status

def test_not_found_and_skip_then_retry(client, db, token):
    job = open_job(db, [("A", "NFJ001"), ("B", "OKJ002")])
    client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    assert client.post(f"/api/jobs/{job}/lookups/NFJ001/status", headers=hdr(token),
                       json={"status": "NOT_FOUND"}).json() == {"status": "NOT_FOUND"}
    client.post(f"/api/jobs/{job}/lookups/OKJ002/status", headers=hdr(token), json={"status": "SKIPPED"})
    rows = db.get_rows(job)
    assert [r["lookup_status"] for r in rows] == [LS.NOT_FOUND, LS.SKIPPED]
    assert rows[0]["error_message"] == "booking not found on the website"
    assert retry_failed(db, job) == 1  # SKIPPED goes back, NOT_FOUND is a result
    assert statuses(db, job) == [LS.NOT_FOUND, LS.NOT_STARTED]
    got = client.post(f"/api/jobs/{job}/claim", headers=hdr(token)).json()["leases"]
    assert [x["pnr"] for x in got] == ["OKJ002"]


def test_blocked_pauses_job_and_resume_requeues(client, db, token):
    job = open_job(db, [("A", "CAPT01"), ("B", "OKK002")])
    client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    client.post(f"/api/jobs/{job}/lookups/CAPT01/status", headers=hdr(token), json={"status": "BLOCKED"})
    j = db.get_job(job)
    assert j["status"] == JobStatus.PAUSED and "CAPTCHA" in j["error_message"]
    r = client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    assert r.status_code == 409 and "paused" in r.json()["detail"]
    # the extension's Resume button (after the person dealt with the page in the browser)
    assert client.post(f"/api/jobs/{job}/resume", headers=hdr(token)).json() == {"status": "READY_FOR_LOOKUP"}
    assert client.post(f"/api/jobs/{job}/resume", headers=hdr(token)).status_code == 409  # not paused now
    assert not resume_job(db, job)
    assert statuses(db, job)[0] == LS.NOT_STARTED
    got = client.post(f"/api/jobs/{job}/claim", headers=hdr(token)).json()["leases"]
    assert "CAPT01" in [x["pnr"] for x in got]


def test_bad_status_rejected(client, db, token):
    job = open_job(db, [("A", "OKL001")])
    url = f"/api/jobs/{job}/lookups/OKL001/status"
    assert client.post(url, headers=hdr(token), json={"status": "PARSED"}).status_code == 400
    assert client.post(url, headers=hdr(token), json={"status": "NOPE"}).status_code == 400


def test_rows_filled_from_gds_screen_are_left_alone(client, db, token):
    job = open_job(db, [("A", "OKM001"), ("B", "OKM002")])
    db.finish_row(db.get_rows(job)[0]["id"], LS.PARSED)
    got = client.post(f"/api/jobs/{job}/claim", headers=hdr(token)).json()["leases"]
    assert [x["pnr"] for x in got] == ["OKM002"]


# -------------------------------------------------------------- progress

def test_progress_counts_and_speed(client, db, token):
    job = open_job(db, [("A", "OKN001"), ("B", "OKN002"), ("C", "OKN003")])
    client.post(f"/api/jobs/{job}/claim", headers=hdr(token))
    client.post(f"/api/jobs/{job}/lookups/OKN001/capture", headers=hdr(token), json={"text": "OKN001 ok"})
    client.post(f"/api/jobs/{job}/lookups/OKN002/status", headers=hdr(token), json={"status": "NOT_FOUND"})
    p = client.get(f"/api/jobs/{job}/progress", headers=hdr(token)).json()
    assert p["pnrs"]["captured"] == 1 and p["pnrs"]["not_found"] == 1 and p["pnrs"]["left"] == 1
    assert p["rows"]["not_found"] == 1 and p["rows"]["in_progress"] == 2
    assert p["per_minute"] > 0 and p["eta_minutes"] is not None


# --------------------------------------------------------- site profiles

def test_site_profiles_list_get_put(client, db, token):
    names = {p["name"]: p for p in client.get("/api/site-profiles", headers=hdr(token)).json()}
    assert names["malaysia_airlines"]["configured"] is True
    assert names["mock_demo"]["configured"] is True
    prof = client.get("/api/site-profile/malaysia_airlines", headers=hdr(token)).json()
    assert prof["result_detect"]["url_contains"] == "/manage-booking/confirmation"
    assert "captcha" in prof["block_detect"]["text_contains"]
    assert 'name="bookingRef"' in prof["fields"]["pnr"]["selector"]
    prof["fields"]["surname"]["selector"] = "TODO"  # a profile with TODO is not usable
    assert client.put("/api/site-profile/malaysia_airlines", headers=hdr(token), json={
        k: v for k, v in prof.items() if k != "configured"}).json()["configured"] is False
    prof["fields"]["surname"]["selector"] = "#lastName"
    prof["fields"]["pnr"]["selector"] = "#bookingRef"
    prof["result_detect"]["selector"] = "#itinerary"
    prof["not_found_detect"]["selector"] = ".error"
    prof.pop("configured")
    r = client.put("/api/site-profile/malaysia_airlines", headers=hdr(token), json=prof)
    assert r.status_code == 200 and r.json()["configured"] is True
    again = client.get("/api/site-profile/malaysia_airlines", headers=hdr(token)).json()
    assert again["fields"]["surname"]["selector"] == "#lastName"  # DB copy wins over the YAML
    assert client.get("/api/site-profile/nope", headers=hdr(token)).status_code == 404


def test_duplicate_rows_do_not_create_extra_lookups(client, db, token):
    job = open_job(db, [("A", "OKP001"), ("A", "OKP001")])  # same surname + first name + PNR
    assert [r["extraction_status"] for r in db.get_rows(job)] == [ExtractionStatus.READY, ExtractionStatus.DUPLICATE]
    got = client.post(f"/api/jobs/{job}/claim", headers=hdr(token)).json()["leases"]
    assert len(got) == 1 and len(got[0]["passengers"]) == 1
