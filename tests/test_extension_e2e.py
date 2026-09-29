"""End to end: real Chromium + the unpacked extension + the extension API + the mock airline site.

The test plays the staff member: it presses Enter on the airline page. Everything else (filling,
detecting, capturing, uploading, going back, filling the next PNR) must happen by itself, and
nothing may be searched before Enter is pressed.

Needs the built extension:  cd extension && npm install && npm run build
"""
import socket
import threading
import time
from pathlib import Path

import pytest

from app import lookups
from app.api import create_app
from app.core.config import SITE_PROFILES_DIR, load_site_profile
from app.core.models import JobStatus, LookupStatus
from app.jobs import start_lookups
from tests.conftest import make_job

DIST = Path(__file__).resolve().parents[1] / "extension" / "dist"
pytestmark = pytest.mark.skipif(not (DIST / "manifest.json").exists(),
                                reason="extension not built: cd extension && npm install && npm run build")

NO_ENTER_WAIT_S = 1.0  # after filling, wait this long to prove nothing searches by itself


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def api_server(settings):
    """The real extension API on a free port. `down["on"] = True` makes it answer 503 (backend blip)."""
    import uvicorn
    from fastapi.responses import JSONResponse

    app = create_app(settings)
    down = {"on": False}

    @app.middleware("http")
    async def _blip(request, call_next):
        if down["on"]:
            return JSONResponse({"detail": "temporarily down"}, status_code=503)
        return await call_next(request)

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", down
    server.should_exit = True
    t.join(5)


@pytest.fixture
def browser(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={DIST}", f"--load-extension={DIST}"])
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        yield ctx, sw
        ctx.close()


def lookup(db, job, pnr):
    return db.conn.execute("SELECT * FROM lookups WHERE job_id=? AND pnr=?", (job, pnr)).fetchone()


def wait_until(fn, timeout=15.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_staff_presses_enter_extension_does_the_rest(settings, db, storage, api_server, mock_site, browser):
    api_url, down = api_server
    base_url, hits = mock_site
    ctx, sw = browser

    job = make_job(db, [("SHUKLA", "OKE2E1"), ("KHAN", "NFE2E2"), ("PATEL", "MISME3"), ("RAO", "OKE2E4"),
                        ("BOSE", "CAPTE5")])
    start_lookups(db, job)
    token = lookups.create_token(db, "e2e")
    profile = load_site_profile(SITE_PROFILES_DIR / "mock_demo.yaml").model_copy(update={"search_url": base_url})
    lookups.save_site_profile(db, "mock_demo", profile)
    sw.evaluate("([u, t]) => chrome.storage.local.set({apiUrl: u, token: t})", [api_url, token])
    ext_id = sw.url.split("/")[2]

    airline = ctx.new_page()
    # Start while a "not found" message is already on screen (from an earlier search): it belongs
    # to no booking of this job, so nothing may be recorded (regression: job 4, 2026-09-24).
    airline.goto(base_url + "search?surname=OLD&pnr=NFOLD9")
    panel = ctx.new_page()
    panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
    panel.wait_for_function("() => document.querySelector('#job option')?.value", timeout=10000)
    panel.select_option("#job", str(job))
    panel.select_option("#profile", "mock_demo")
    panel.click("#start")
    airline.bring_to_front()
    time.sleep(2)
    assert db.conn.execute("SELECT COUNT(*) FROM lookups WHERE job_id=? AND status<>'CLAIMED'",
                           (job,)).fetchone()[0] == 0, "recorded a result nobody searched for"
    airline.goto(base_url)  # the person opens the search form

    # The mock form sits behind a "My booking" tab (like MH): the extension opens the tab, and
    # filled_with only accepts values in the *visible* form, so it also checks the tab opened.
    def filled_with(pnr, surname):
        airline.wait_for_function(
            "([p, s]) => { const v = (sel) => [...document.querySelectorAll(sel)].find((e) => e.offsetParent)?.value;"
            " return v('#pnr') === p && v('#surname') === s; }",  # the visible form, not the hidden copy
            arg=[pnr, surname], timeout=10000, polling=100)
        time.sleep(NO_ENTER_WAIT_S)
        assert hits[pnr] == 0, f"{pnr} was searched before Enter was pressed"
        assert airline.evaluate("document.activeElement?.id") == "pnr"  # cursor ready for Enter

    # 1. normal booking: Enter -> result captured -> back on the form with the next PNR
    filled_with("OKE2E1", "SHUKLA")
    airline.keyboard.press("Enter")
    airline.wait_for_selector("#result")
    t_result = time.monotonic()
    airline.wait_for_function("() => [...document.querySelectorAll('#pnr')].find((e) => e.offsetParent)?.value === 'NFE2E2'", timeout=10000)
    overhead_ms = (time.monotonic() - t_result) * 1000 - profile.settle_ms
    print(f"\nresult shown -> next booking filled: {overhead_ms:.0f} ms (excluding settle_ms)")
    assert overhead_ms < 1000  # includes the local page load; the extension's own work is a fraction
    wait_until(lambda: lookup(db, job, "OKE2E1")["status"] == LookupStatus.CAPTURED, what="capture upload")
    page = storage.read_bytes(lookup(db, job, "OKE2E1")["page_text_path"]).decode()
    assert "OKE2E1" in page
    assert "COLLAPSED / HIDDEN SECTIONS" in page and "shukla@example.com" in page  # folded section too
    assert "Passport number: [removed]" in page and "13/08/1993" not in page  # personal data not stored
    assert hits["OKE2E1"] == 1

    # 2. not found -> NOT_FOUND automatically
    filled_with("NFE2E2", "KHAN")
    airline.keyboard.press("Enter")
    filled_with("MISME3", "PATEL")
    wait_until(lambda: lookup(db, job, "NFE2E2")["status"] == LookupStatus.NOT_FOUND, what="not found")

    # 3. the site shows another booking -> MISMATCH, not saved
    airline.keyboard.press("Enter")
    filled_with("OKE2E4", "RAO")
    wait_until(lambda: lookup(db, job, "MISME3")["status"] == LookupStatus.MISMATCH, what="mismatch")
    assert lookup(db, job, "MISME3")["page_text_path"] is None

    # 4. backend down while capturing: kept in the outbox, uploaded when it's back
    down["on"] = True
    airline.keyboard.press("Enter")
    filled_with("CAPTE5", "BOSE")  # staff keep working meanwhile
    assert lookup(db, job, "OKE2E4")["status"] == LookupStatus.CLAIMED
    down["on"] = False
    wait_until(lambda: lookup(db, job, "OKE2E4")["status"] == LookupStatus.CAPTURED, timeout=40,
               what="outbox retry after the backend came back")

    # 5. CAPTCHA -> BLOCKED, the job pauses, the panel tells the person what to do
    airline.keyboard.press("Enter")
    wait_until(lambda: db.get_job(job)["status"] == JobStatus.PAUSED, what="job paused on CAPTCHA")
    assert lookup(db, job, "CAPTE5")["status"] == LookupStatus.BLOCKED
    panel.wait_for_function("() => !document.querySelector('#warning').hidden")
    assert "Solve it in the browser" in panel.inner_text("#warning")

    # each PNR was searched exactly once, only by the Enter presses above
    assert [hits[p] for p in ("OKE2E1", "NFE2E2", "MISME3", "OKE2E4", "CAPTE5")] == [1, 1, 1, 1, 1]


def test_auto_continue_searches_by_itself_paced_and_stops_at_captcha(settings, db, storage, api_server,
                                                                     mock_site, browser):
    api_url, _ = api_server
    base_url, hits = mock_site
    ctx, sw = browser

    pnrs = ["OKAU01", "NFAU02", "OKAU03", "CAPTA4", "OKAU05"]
    job = make_job(db, [("SHUKLA", pnrs[0]), ("KHAN", pnrs[1]), ("RAO", pnrs[2]), ("BOSE", pnrs[3]),
                        ("DAS", pnrs[4])])
    start_lookups(db, job)
    token = lookups.create_token(db, "e2e-auto")
    profile = load_site_profile(SITE_PROFILES_DIR / "mock_demo.yaml").model_copy(update={"search_url": base_url})
    gap_s = profile.auto_submit.min_gap_ms / 1000
    lookups.save_site_profile(db, "mock_demo", profile)
    sw.evaluate("([u, t]) => chrome.storage.local.set({apiUrl: u, token: t})", [api_url, token])
    ext_id = sw.url.split("/")[2]

    airline = ctx.new_page()
    airline.goto(base_url)
    panel = ctx.new_page()
    panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
    panel.wait_for_function("() => document.querySelector('#job option')?.value", timeout=10000)
    panel.select_option("#job", str(job))
    panel.select_option("#profile", "mock_demo")
    panel.click("label.switch .track")  # staff switch Auto-continue on; nobody presses Enter in this test
    panel.click("#start")
    airline.bring_to_front()
    t0 = time.monotonic()

    def status(pnr):
        row = lookup(db, job, pnr)
        return row["status"] if row else None  # not claimed yet

    wait_until(lambda: status(pnrs[0]) == LookupStatus.CAPTURED, timeout=30, what="1st capture")
    wait_until(lambda: status(pnrs[1]) == LookupStatus.NOT_FOUND, timeout=30, what="not found")
    wait_until(lambda: status(pnrs[2]) == LookupStatus.CAPTURED, timeout=30, what="3rd capture")
    wait_until(lambda: db.get_job(job)["status"] == JobStatus.PAUSED, timeout=30, what="paused on CAPTCHA")
    elapsed = time.monotonic() - t0
    assert lookup(db, job, pnrs[3])["status"] == LookupStatus.BLOCKED
    assert elapsed >= 3 * gap_s - 0.5, f"4 searches in {elapsed:.1f} s: not paced by min_gap_ms"

    # Auto-continue switched itself off at the CAPTCHA, and nothing after it was searched.
    panel.wait_for_function("() => !document.querySelector('#auto').checked")
    time.sleep(gap_s + 1)
    assert [hits[p] for p in pnrs] == [1, 1, 1, 1, 0]
    assert "COLLAPSED / HIDDEN SECTIONS" in storage.read_bytes(lookup(db, job, pnrs[0])["page_text_path"]).decode()
