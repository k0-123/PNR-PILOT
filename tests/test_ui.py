"""Streamlit UI smoke tests (headless AppTest against a temporary database)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.core.config import get_settings
from app.core.models import ExtractionStatus, JobStatus, LookupStatus
from tests.conftest import make_job

EMAIL, PASSWORD = "tester@example.com", "s3cret-pass"
UI = str(Path(__file__).resolve().parents[1] / "app" / "ui.py")


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("APP_EMAIL", EMAIL)
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def signed_in(page="Dashboard", job=None):
    at = AppTest.from_file(UI, default_timeout=30)
    at.session_state["signed_in_as"] = EMAIL
    at.session_state["_goto"] = page
    if job is not None:
        at.session_state["_goto_job"] = job
    return at.run()


def no_errors(at):
    assert not at.exception, [e.value for e in at.exception]


def test_sign_in_required_and_works(app):
    at = AppTest.from_file(UI, default_timeout=30).run()
    assert len(at.text_input) == 2 and not at.get("radio")  # only the sign-in form
    at.text_input[0].input(EMAIL.upper())
    at.text_input[1].input(PASSWORD)
    at.button[0].click().run()
    no_errors(at)
    assert at.session_state["signed_in_as"] == EMAIL
    assert at.title[0].value == "Dashboard"


def test_wrong_password_rejected(app):
    at = AppTest.from_file(UI, default_timeout=30).run()
    at.text_input[0].input(EMAIL)
    at.text_input[1].input("nope")
    at.button[0].click().run()
    assert "Wrong email or password." in [e.value for e in at.error]
    assert "signed_in_as" not in at.session_state


def test_dashboard_lists_jobs(app, db):
    make_job(db, [("A", "OKY001")], status=JobStatus.COMPLETED, name="first")
    at = signed_in()
    no_errors(at)
    kpis = next(m.value for m in at.markdown if 'class="kpis' in m.value)
    assert "Jobs" in kpis and "Rows looked up" in kpis
    assert len(at.dataframe) == 1
    assert any("worker is not running" in w.value for w in at.warning)


def test_new_job_page_has_no_website_picker(app):
    at = signed_in("New job")
    no_errors(at)
    assert not at.selectbox  # lookups happen in the browser extension, not on a configured website


def test_job_details_actions(app, db):
    job = make_job(db, [("A", "OKZ001"), ("B", "OKZ002")], status=JobStatus.READY_FOR_LOOKUP)
    at = signed_in("Job details", job)
    no_errors(at)
    labels = [b.label for b in at.button]
    assert "▶ Open for lookups" in labels
    next(b for b in at.button if b.label == "▶ Open for lookups").click().run()
    no_errors(at)
    j = db.get_job(job)
    assert (j["status"], j["lookup_requested"]) == (JobStatus.READY_FOR_LOOKUP, 1)
    assert "⏸ Pause" in [b.label for b in at.button]
    next(b for b in at.button if b.label == "⏸ Pause").click().run()
    assert db.get_job(job)["status"] == JobStatus.PAUSED
    assert "▶ Resume" in [b.label for b in at.button]


def test_review_page_validates_and_approves(app, db):
    job = make_job(db, [("SHUKLA", "OKR101"), ("PATEL", None)], status=JobStatus.AWAITING_REVIEW)
    at = signed_in("Review", job)
    no_errors(at)
    # one review form: surname, first name, PNR inputs
    pnr_box = next(t for t in at.text_input if t.label == "PNR")
    pnr_box.input("AB1")
    next(b for b in at.button if b.label == "✔ Approve").click().run()
    assert any("PNR must be exactly 6" in e.value for e in at.error)
    assert db.get_rows(job)[1]["extraction_status"] == ExtractionStatus.NEEDS_REVIEW

    next(t for t in at.text_input if t.label == "PNR").input("OKR102")
    next(b for b in at.button if b.label == "✔ Approve").click().run()
    no_errors(at)
    r = db.get_rows(job)[1]
    assert (r["extraction_status"], r["edited_pnr"]) == (ExtractionStatus.APPROVED, "OKR102")
    assert db.get_job(job)["status"] == JobStatus.READY_FOR_LOOKUP
    assert "Nothing to review" in at.success[-1].value


def test_results_page_downloads(app, db):
    job = make_job(db, [("SHUKLA", "OKS101"), ("PATEL", "OKS102")], status=JobStatus.COMPLETED)
    rows = db.get_rows(job)
    db.save_lookup_result(rows[0]["id"], result={"flight_numbers": "AI 131"}, page_text_path=None,
                          screenshot_path=None)
    db.finish_row(rows[0]["id"], LookupStatus.PARSED, attempts=1)
    db.finish_row(rows[1]["id"], LookupStatus.NOT_FOUND, attempts=1)
    at = signed_in("Results", job)
    no_errors(at)
    labels = [b.label for b in at.get("download_button")]
    assert "⬇️ Final Excel" in labels and "⬇️ Final CSV" in labels
    df = at.dataframe[0].value
    assert list(df["Lookup Status"]) == ["PARSED", "NOT_FOUND"]
    assert df["Flight Number(s)"].iloc[0] == "AI 131"
    at.text_input[0].input("PATEL").run()
    assert len(at.dataframe[0].value) == 1


def test_job_details_offers_pnr_screen_upload(app, db):
    job = make_job(db, [("CHEN", "ER7P5B")], status=JobStatus.READY_FOR_LOOKUP)
    db.add_pnr_screen(job, "rt.png", f"screens/job_{job}/rt.png", "image/png", "abc")
    at = signed_in("Job details", job)
    no_errors(at)
    assert any("GDS PNR screens" in e.label for e in at.expander)
    assert any("GDS PNR screens" in m.value for m in at.markdown)  # screens table in live section


def test_extension_tokens_page_creates_and_revokes(app, db):
    from app import lookups
    at = signed_in("Extension tokens")
    no_errors(at)
    next(t for t in at.text_input if t.label == "Staff member / device name").input("Asha")
    next(b for b in at.button if b.label == "Create token").click().run()
    no_errors(at)
    shown = at.code[0].value
    assert shown.startswith("gds_") and lookups.authenticate(db, shown)["name"] == "Asha"
    next(b for b in at.button if b.label == "Revoke").click().run()
    assert lookups.authenticate(db, shown) is None


def test_results_page_fix_and_retry(app, db):
    job = make_job(db, [("SHUKLA", "OKX101"), ("PATEL", "OKX1O2")], status=JobStatus.COMPLETED_WITH_ERRORS)
    rows = db.get_rows(job)
    db.finish_row(rows[0]["id"], LookupStatus.PARSED, attempts=1)
    db.finish_row(rows[1]["id"], LookupStatus.NOT_FOUND, attempts=1)
    at = signed_in("Results", job)
    no_errors(at)
    assert any("Fix & retry" in e.label for e in at.expander)
    next(t for t in at.text_input if t.label == "PNR").input("OKX102")
    next(b for b in at.button if b.label == "🔁 Fix & retry").click().run()
    no_errors(at)
    r = db.get_row(rows[1]["id"])
    assert (r["edited_pnr"], r["lookup_status"]) == ("OKX102", LookupStatus.NOT_STARTED)
    assert db.get_job(job)["status"] == JobStatus.READY_FOR_LOOKUP


def test_flt_credits_only_admin_can_add(app, db, monkeypatch):
    monkeypatch.setenv("FLT_ADMIN_EMAIL", "boss@example.com")  # the signed-in tester isn't the admin
    get_settings.cache_clear()
    at = signed_in("FLT credits")
    no_errors(at)
    assert not [b for b in at.button if b.label == "➕ Add plan"]
    assert any("Only the FLT admin can add FLT" in c.value for c in at.caption)
    assert not any("AI cost" in m.value for m in at.markdown)


def test_flt_credits_page_adds_plan_and_blocks_short_jobs(app, db, monkeypatch):
    from app import credits
    monkeypatch.setenv("FLT_ADMIN_EMAIL", EMAIL.upper())  # the admin (email match ignores case)
    get_settings.cache_clear()
    at = signed_in("FLT credits")
    no_errors(at)
    assert any("FLT credits are off" in i.value for i in at.info)
    next(b for b in at.button if b.label == "➕ Add plan").click().run()  # default choice: Plan 10k
    no_errors(at)
    assert credits.balance(db).balance == 10_000
    assert any("Plan added" in str(df.value["Type"].tolist()) for df in at.dataframe)

    credits.add_plan(db, 1)  # use nearly everything so the next job doesn't fit
    db.conn.execute("INSERT INTO flt_ledger(created_at, kind, amount) VALUES ('2026-09-25', 'adjust', -10000)")
    db.conn.commit()
    job = make_job(db, [("A", "OKU001"), ("B", "OKU002")], status=JobStatus.READY_FOR_LOOKUP)
    at = signed_in("Job details", job)
    next(b for b in at.button if b.label == "▶ Open for lookups").click().run()
    no_errors(at)
    assert any("Needs 2 FLT, you have 1" in e.value for e in at.error)
    assert db.get_job(job)["lookup_requested"] == 0
