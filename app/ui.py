"""Streamlit UI (PLAN section 14).

    streamlit run app/ui.py          (and, in another terminal: python -m app.worker)

The UI only reads/writes SQLite and storage. Extraction runs in the worker process, so jobs
keep going when the browser tab is closed. Lookups are done by staff in the browser extension.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `streamlit run app/ui.py`

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import credits, jobs, lookups, theme, users  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.core.logging_setup import setup_logging  # noqa: E402
from app.core.models import LOOKUP_RETRYABLE, ExtractionStatus, JobStatus, LookupStatus  # noqa: E402
from app.core.storage import LocalStorage, StorageError  # noqa: E402
from app.excel.writer import table_to_csv_bytes  # noqa: E402
from app.lookup.gds_screens import unmatched_list  # noqa: E402

st.set_page_config(page_title="PNR Pilot · FocusLinkTech", page_icon="✈️", layout="wide")
st.markdown(theme.CSS, unsafe_allow_html=True)

settings = get_settings()
log = logging.getLogger("app.ui")


@st.cache_resource
def _init_logging() -> bool:
    setup_logging(settings.log_level, settings.secret_values())
    return True


_init_logging()
db = Database(settings.db_path)
storage = LocalStorage(settings.data_dir)
users.ensure_bootstrap_admin(db, settings)

PAGES = ["Dashboard", "New job", "Job details", "Review", "Results", "FLT credits", "Extension tokens"]
BADGE = {
    JobStatus.PENDING: "gray", JobStatus.EXTRACTING: "blue", JobStatus.AWAITING_REVIEW: "orange",
    JobStatus.READY_FOR_LOOKUP: "violet", JobStatus.LOOKING_UP: "blue", JobStatus.PAUSED: "orange",
    JobStatus.COMPLETED: "green", JobStatus.COMPLETED_WITH_ERRORS: "orange", JobStatus.FAILED: "red",
    JobStatus.CANCELLED: "gray",
}
ROW_COLORS = {
    LookupStatus.PARSED: "#d9f2d9", LookupStatus.NOT_FOUND: "#fff2c2", LookupStatus.CLAIMED: "#dbe9ff",
    LookupStatus.CAPTURED: "#dbe9ff",
    ExtractionStatus.NEEDS_REVIEW: "#fff2c2", ExtractionStatus.DUPLICATE: "#e6e6e6",
    ExtractionStatus.REJECTED: "#eeeeee",
}
FAILED_STATUSES = {s.value for s in LOOKUP_RETRYABLE}


def badge(status: str) -> str:
    return f":{BADGE.get(status, 'gray')}-badge[{status.replace('_', ' ')}]"


def goto(page: str, job_id: int | None = None) -> None:
    st.session_state["_goto"] = page
    if job_id is not None:
        st.session_state["_goto_job"] = job_id
    st.rerun()


def flash(kind: str, msg: str) -> None:
    """Show a message after the next rerun."""
    st.session_state.setdefault("_flash", []).append((kind, msg))


def intro(text: str) -> None:
    st.markdown(f'<p class="intro">{text}</p>', unsafe_allow_html=True)


def show_flash() -> None:
    for kind, msg in st.session_state.pop("_flash", []):
        getattr(st, kind)(msg)


# ------------------------------------------------------------------ sign in
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


@st.cache_resource
def _sign_in_guard() -> dict:
    """App-wide failed-attempt counter (survives page refreshes, unlike session_state)."""
    return {"failed": 0, "locked_until": 0.0}


_AURORA_VIDEO = ("https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/"
                 "hf_20260506_081238_406ed0e3-5d83-436e-a512-0bbff7ec5b95.mp4")

_AURORA_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
.stApp, [data-testid="stAppViewContainer"]{background:#000!important;font-family:'Inter',sans-serif;}
#MainMenu,header,footer{visibility:hidden;}
.block-container{padding-top:0!important;}
.aur-hero{position:fixed;top:16px;left:16px;bottom:16px;width:49vw;border-radius:24px;overflow:hidden;
  background:linear-gradient(135deg,#4338ca 0%,#7c3aed 50%,#2563eb 100%);
  box-shadow:0 20px 60px rgba(0,0,0,.6);z-index:0;}
.aur-hero video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;}
.aur-inner{position:absolute;z-index:2;left:48px;bottom:120px;width:320px;color:#fff;}
.aur-brand{display:flex;align-items:center;gap:8px;font-weight:600;font-size:20px;margin-bottom:28px;}
.aur-dot{width:16px;height:16px;border-radius:50%;background:#fff;display:inline-block;}
.aur-h{font-size:38px;font-weight:500;letter-spacing:-1px;margin:0;}
.aur-p{color:rgba(255,255,255,.6);font-size:14px;margin:8px 0 24px;}
.aur-step{display:flex;align-items:center;gap:12px;padding:12px 16px;border-radius:14px;margin-bottom:10px;
  background:#1A1A1A;color:#fff;font-size:14px;font-weight:500;}
.aur-step.on{background:#fff;color:#000;}
.aur-num{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:13px;background:rgba(255,255,255,.1);color:rgba(255,255,255,.4);}
.aur-step.on .aur-num{background:#000;color:#fff;}
.aur-head{margin:0 0 8px;} .aur-title{font-size:30px;font-weight:500;color:#fff;letter-spacing:-.5px;}
.aur-sub{color:rgba(255,255,255,.4);font-size:14px;margin-top:4px;}
section[data-testid="stMain"] label{color:#fff!important;font-weight:500;}
div[data-testid="stTextInput"] input{background:#1A1A1A!important;color:#fff!important;border:none!important;
  border-radius:12px!important;height:44px;}
button[kind="primary"]{background:#fff!important;color:#000!important;border:none!important;
  border-radius:12px!important;height:52px;font-weight:600!important;}
.aur-foot{color:rgba(255,255,255,.5);font-size:13px;text-align:center;margin-top:14px;}
.aur-foot b{color:#fff;}
</style>"""


def _aurora_hero() -> str:
    steps = [("1", "Register your identity", True), ("2", "Configure your studio", False),
             ("3", "Finalize your profile", False)]
    rows = "".join(
        f'<div class="aur-step{" on" if a else ""}"><span class="aur-num">{n}</span>{t}</div>'
        for n, t, a in steps)
    return (f'<div class="aur-hero">'
            f'<div class="aur-inner"><div class="aur-brand"><span class="aur-dot"></span>PNR Pilot</div>'
            f'<h1 class="aur-h">Join PNR&nbsp;Pilot</h1>'
            f'<p class="aur-p">Built &amp; developed by <b>focuslinktech.com</b></p>{rows}</div></div>')


def sign_in_gate() -> None:
    """Admin-created accounts only, one active device each (a new sign-in signs the old one out)."""
    # Already signed in this browser session: make sure this is still the user's active device.
    if st.session_state.get("user_id"):
        if users.session_valid(db, st.session_state["user_id"], st.session_state.get("session_id")):
            return
        # The session was taken over by another device (or the account was disabled/reset).
        for key in ("signed_in_as", "user_id", "session_id", "is_admin"):
            st.session_state.pop(key, None)
        flash("warning", "You were signed out because this account signed in on another device.")

    st.markdown(_AURORA_CSS + _aurora_hero(), unsafe_allow_html=True)
    _, mid = st.columns([1.08, 1])
    with mid:
        st.markdown(
            '<div class="aur-head"><div class="aur-title">Sign in to PNR&nbsp;Pilot</div>'
            '<div class="aur-sub">Enter your details to access your workspace.</div></div>',
            unsafe_allow_html=True)
        show_flash()
        guard = _sign_in_guard()
        if time.time() < guard["locked_until"]:
            st.error("Too many wrong attempts. Try again in "
                     f"{int(guard['locked_until'] - time.time()) + 1} s.")
            st.stop()
        with st.form("sign_in"):
            email = st.text_input("Email", autocomplete="email")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                user = users.authenticate_login(db, email, password)
                if user is not None:
                    st.session_state.signed_in_as = user.email
                    st.session_state.user_id = user.id
                    st.session_state.is_admin = user.is_admin
                    st.session_state.session_id = users.start_session(db, user.id)
                    guard["failed"] = 0
                    log.info("Sign-in succeeded", extra={"email": user.email})
                    st.rerun()
                guard["failed"] += 1
                log.warning("Sign-in failed", extra={"attempt": guard["failed"]})
                if guard["failed"] >= MAX_ATTEMPTS:
                    guard["locked_until"] = time.time() + LOCKOUT_SECONDS
                    guard["failed"] = 0
                    st.rerun()
                st.error("Wrong email or password.")

        pills = "".join(
            f'<span style="background:#1A1A1A;color:#fff;padding:.3rem .7rem;border-radius:999px;'
            f'font-size:.78rem;font-weight:500;border:1px solid rgba(255,255,255,.1)">{s}</span>'
            for s in ("Web & App Development", "Business Automation", "AI & Data Solutions",
                      "Chrome Extensions", "Cloud Hosting & Deployment"))
        st.markdown(
            '<div style="margin-top:1.4rem;padding-top:1rem;border-top:1px solid rgba(255,255,255,.1)">'
            '<div style="font-size:.72rem;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;'
            'color:rgba(255,255,255,.4);margin-bottom:.6rem;text-align:center">Services by focuslinktech.com</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:.4rem;justify-content:center">{pills}</div>'
            '<div class="aur-foot" style="margin-top:.9rem">Need custom software? &nbsp;<b>focuslinktech.com</b>'
            '</div></div>', unsafe_allow_html=True)
    st.stop()


sign_in_gate()
CURRENT_USER_ID = st.session_state["user_id"]
IS_ADMIN = st.session_state.get("is_admin", False)


# ------------------------------------------------------- getting started
def _extension_zip() -> bytes:
    import io, zipfile
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / "extension" / "dist"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in root.rglob("*"):
            if f.is_file():
                z.write(f, f"pnr-pilot-extension/{f.relative_to(root)}")
    return buf.getvalue()


def getting_started_gate() -> None:
    if st.session_state.get("onboarded"):
        return
    api_url = "http://64.227.130.82:8000"
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown(theme.brand("lg"), unsafe_allow_html=True)
        st.title("Getting started")
        st.caption("Do this once to enable airline lookups in your browser.")

        st.markdown("### 1 · Download the extension")
        try:
            st.download_button("⬇ Download extension (.zip)", _extension_zip(),
                               file_name="pnr-pilot-extension.zip", type="primary")
        except Exception as exc:
            st.error(f"Download unavailable: {exc}")
        st.caption("Unzip it somewhere permanent (don't delete the folder afterwards).")

        st.markdown("### 2 · Install it in Chrome")
        st.markdown("- Open **chrome://extensions**\n- Turn on **Developer mode** (top-right)\n"
                    "- Click **Load unpacked** → pick the unzipped **pnr-pilot-extension** folder")

        st.markdown("### 3 · Connect the extension")
        st.markdown("Open the extension's side panel → ⚙ settings, and paste:")
        st.code(f"API address: {api_url}\nToken: ask your admin (Extension tokens page)")

        st.divider()
        if st.button("Next → Open dashboard", type="primary", width="stretch"):
            st.session_state["onboarded"] = True
            st.rerun()
        st.caption("You can reopen this from the sidebar anytime.")
    st.stop()


getting_started_gate()


# --------------------------------------------------------------- sidebar
if "_goto" in st.session_state:
    st.session_state["page"] = st.session_state.pop("_goto")
if "_goto_job" in st.session_state:
    st.session_state["current_job"] = st.session_state.pop("_goto_job")

all_jobs = db.list_jobs_with_counts(CURRENT_USER_ID)  # each user sees only their own jobs
job_ids = [j["id"] for j in all_jobs]
job_by_id = {j["id"]: j for j in all_jobs}
if st.session_state.get("current_job") not in job_ids:
    st.session_state["current_job"] = job_ids[0] if job_ids else None

with st.sidebar:
    st.markdown(theme.brand(), unsafe_allow_html=True)
    visible_pages = PAGES + (["Manage users"] if IS_ADMIN else [])
    page = st.radio("Page", visible_pages, key="page", label_visibility="collapsed")
    if job_ids and page in ("Job details", "Review", "Results"):
        # No widget key: the chosen job lives in session_state["current_job"] and survives pages
        # where this selectbox isn't shown.
        st.session_state["current_job"] = st.selectbox(
            "Job", job_ids, index=job_ids.index(st.session_state["current_job"]),
            format_func=lambda i: f"#{i} · {job_by_id[i]['name']} · {job_by_id[i]['status']}")
    st.divider()
    online = db.workers_online(30)
    st.caption(("🟢 Worker running" if online else "🔴 Worker not running"))
    st.caption(f"Extraction: `{settings.gemini_model_extract}`  \nResults: `{settings.gemini_model_result}`")
    flt = credits.balance(db, CURRENT_USER_ID)
    st.caption(f"FLT credits: **{flt.available:,}** available" if flt.active else "FLT credits: off (no plan yet)")
    st.caption(f"Signed in as **{st.session_state.signed_in_as}**" + ("  ·  _admin_" if IS_ADMIN else ""))
    if st.button("Sign out", width="stretch"):
        users.end_session(db, CURRENT_USER_ID)
        for key in ("signed_in_as", "user_id", "session_id", "is_admin", "current_job"):
            st.session_state.pop(key, None)
        st.rerun()

if not online:
    st.warning("The background worker is not running, so new jobs will wait. Start it with "
               "`python -m app.worker` (or double-click `start_local.bat`).", icon="⚠️")
if flt.warning:
    st.warning(flt.warning, icon="🪙")
show_flash()


# ------------------------------------------------------------- dashboard
def page_dashboard() -> None:
    st.title("Dashboard")
    intro("Every GDS photo is read, checked and looked up, then turned into one final Excel.")
    rows_done = sum(j["rows_success"] + j["rows_not_found"] + j["rows_failed"] for j in all_jobs)
    st.markdown(theme.kpi_row([
        ("Jobs", len(all_jobs)),
        ("Rows looked up", rows_done),
        ("Success", sum(j["rows_success"] for j in all_jobs)),
        ("Failed", sum(j["rows_failed"] for j in all_jobs)),
        ("FLT left", flt.available if flt.active else "–"),
    ], animate=True), unsafe_allow_html=True)

    if st.button("➕ New job", type="primary"):
        goto("New job")
    if not all_jobs:
        st.info("No jobs yet. Create one with **New job**.")
        return
    df = pd.DataFrame([{
        "ID": j["id"], "Name": j["name"], "Status": j["status"], "Images": j["images"],
        "Rows": j["rows_total"], "Review": j["rows_review"], "Success": j["rows_success"],
        "Not found": j["rows_not_found"], "Failed": j["rows_failed"],
        "Cost ($)": round(j["total_cost_usd"], 4), "Created": j["created_at"][:16].replace("T", " "),
    } for j in all_jobs])
    st.caption("Click a row to open the job.")
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="jobs_table")
    sel = event.selection.rows if event and event.selection else []
    if sel:
        st.session_state.pop("jobs_table", None)  # don't re-open it when coming back
        goto("Job details", int(df.iloc[sel[0]]["ID"]))


# --------------------------------------------------------------- new job
def page_new_job() -> None:
    st.title("New job")
    intro("Upload the GDS photos. The worker reads every passenger row, flags anything unsure for "
          "review, and keeps going if you close this tab.")

    with st.form("new_job", clear_on_submit=True):
        files = st.file_uploader(f"GDS photos (PNG, JPG, JPEG, WEBP · max {settings.max_file_size_mb:g} MB each)",
                                 type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
        name = st.text_input("Job name (optional)")
        submitted = st.form_submit_button("Create job", type="primary")
    st.caption(f"After you create the job, the worker reads the images, {settings.gemini_concurrency} at a time. "
               "You can close this tab; the job keeps running.")
    if submitted:
        if not files:
            st.warning("Choose at least one image.")
            return
        try:
            job_id = jobs.create_job(db, storage, settings, name.strip() or Path(files[0].name).stem,
                                     [(f.name, f.getvalue()) for f in files], user_id=CURRENT_USER_ID)
        except (StorageError, jobs.JobActionError) as exc:
            st.error(str(exc))
            return
        flash("success", f"Job #{job_id} created. The worker is reading the images.")
        goto("Job details", job_id)


# ----------------------------------------------------------- job details
def _job_actions(job, p: jobs.Progress) -> None:
    status = job["status"]
    # Which controls apply right now. Results is always shown; the rest depend on job state.
    show_open = status in jobs.LOOKUP_STARTABLE and not job["lookup_requested"] and bool(p.rows_total)
    show_review = bool(p.review)
    show_pause = status in (JobStatus.LOOKING_UP, JobStatus.READY_FOR_LOOKUP) and job["lookup_requested"]
    show_resume = status == JobStatus.PAUSED
    show_retry = bool(p.failed)
    show_cancel = status in jobs.CANCELLABLE
    count = sum([show_open, show_review, show_pause, show_resume, show_retry, show_cancel, True])
    # One column per button so labels never truncate; pad to a minimum of 3 slots so a lone button
    # isn't stretched across the whole row.
    weights = [1] * count + ([3 - count] if count < 3 else [])
    cols = iter(st.columns(weights))

    if show_open:
        label = "▶ Open for lookups" + (f" (skip {p.review} unreviewed)" if p.review else "")
        if next(cols).button(label, type="primary", width="stretch"):
            try:
                jobs.start_lookups(db, job["id"])
                flash("success", "Staff can now pick this job in the browser extension's side panel.")
            except jobs.JobActionError as exc:
                flash("error", str(exc))
            st.rerun()
    if show_review and next(cols).button(f"📝 Review {p.review} row(s)", width="stretch"):
        goto("Review")
    if show_pause and next(cols).button("⏸ Pause", width="stretch"):
        jobs.pause_job(db, job["id"])
        st.rerun()
    if show_resume and next(cols).button("▶ Resume", type="primary", width="stretch"):
        jobs.resume_job(db, job["id"])
        st.rerun()
    if show_retry and next(cols).button(f"🔁 Retry {p.failed} failed", width="stretch",
                                        help="Pages that couldn't be read are read again (no new search); "
                                             "skipped / blocked / mismatched bookings go back to the extension"):
        try:
            flash("success", f"{jobs.retry_failed(db, job['id'])} row(s) queued again.")
        except jobs.JobActionError as exc:
            flash("error", str(exc))
        st.rerun()
    if show_cancel:
        with next(cols).popover("✖ Cancel", width="stretch"):
            st.write("Stop this job? Rows already done are kept.")
            if st.button("Yes, cancel job", type="primary"):
                jobs.cancel_job(db, job["id"])
                st.rerun()
    if next(cols).button("📥 Results", width="stretch"):
        goto("Results")

    saved_pages = db.conn.execute("SELECT COUNT(*) FROM lookups WHERE job_id=? AND page_text_path IS NOT NULL "
                                  "AND status IN ('PARSED','PARSE_ERROR')", (job["id"],)).fetchone()[0]
    if saved_pages:
        with st.popover("📄 Re-read pages", width="content"):
            st.write(f"Read the {saved_pages} saved website page(s) again, e.g. after changing the result "
                     "fields. No booking is searched again.")
            if st.button("Re-read all saved pages", type="primary"):
                n = jobs.reparse_all(db, job["id"])
                flash("success", f"{n} page(s) queued; the worker reads them in a few seconds.")
                st.rerun()


def _pnr_screens_upload(job) -> None:
    """Booking details from GDS PNR display screenshots (fallback when the website can't be used)."""
    if job["status"] in (JobStatus.PENDING, JobStatus.EXTRACTING, JobStatus.CANCELLED) or not db.get_rows(job["id"]):
        return
    with st.expander("📄 Add booking details from GDS PNR screens (full name, e-ticket, frequent flyer, "
                     "contact, route)", expanded=not db.get_pnr_screens(job["id"])):
        st.caption("Take a screenshot of the full PNR display (e.g. Amadeus `RT`) for each booking and upload "
                   "them here. The worker reads each screen and fills the matching rows by PNR + surname.")
        with st.form(f"screens_{job['id']}", clear_on_submit=True):
            files = st.file_uploader("PNR display screenshots", type=["png", "jpg", "jpeg", "webp"],
                                     accept_multiple_files=True)
            if st.form_submit_button("Read screens", type="primary"):
                if not files:
                    st.warning("Choose at least one screenshot.")
                else:
                    try:
                        n = len(jobs.add_pnr_screens(db, storage, settings, job["id"],
                                                     [(f.name, f.getvalue()) for f in files]))
                        flash("success", f"{n} screen(s) queued. The worker reads them in a few seconds.")
                    except (StorageError, jobs.JobActionError) as exc:
                        flash("error", str(exc))
                    st.rerun()


def _rows_frame(rows) -> pd.DataFrame:
    return pd.DataFrame([{
        "Image": r["image_filename"], "Line": r["line_no"], "Surname": r["effective_surname"],
        "PNR": r["effective_pnr"], "Extraction": r["extraction_status"],
        "Lookup": r["lookup_status"] if r["extraction_status"] in ("READY", "APPROVED") else "",
        "Attempts": r["attempts"] or "", "Error / notes": r["error_message"] or r["notes"] or "",
    } for r in rows])


def _style_rows(df: pd.DataFrame):
    def colour(row):
        key = row["Lookup"] or row["Extraction"]
        bg = ROW_COLORS.get(key) or ("#f8cbad" if key in FAILED_STATUSES else "")
        return [f"background-color: {bg}; color: #111" if bg else ""] * len(row)
    return df.style.apply(colour, axis=1)


def page_job_details(job_id: int) -> None:
    job = db.get_job(job_id)
    st.title(f"Job #{job_id}: {job['name']}")
    st.markdown(f"{badge(job['status'])} &nbsp; created {job['created_at'][:16].replace('T', ' ')} UTC")
    if job["error_message"]:
        (st.error if job["status"] == JobStatus.FAILED else st.info)(job["error_message"])
    _job_actions(job, jobs.progress(db, job_id))
    _pnr_screens_upload(job)

    screens_busy = any(s["status"] in ("PENDING", "PROCESSING") for s in db.get_pnr_screens(job_id))
    active = screens_busy or job["status"] in (JobStatus.PENDING, JobStatus.EXTRACTING, JobStatus.LOOKING_UP) or (
        job["status"] == JobStatus.READY_FOR_LOOKUP and job["lookup_requested"])

    @st.fragment(run_every=3 if active else None)
    def live() -> None:
        current = db.get_job(job_id)
        if current["status"] != job["status"]:
            st.rerun()  # status changed: refresh the whole page (buttons etc.)
        p = jobs.progress(db, job_id)
        a, b = st.columns(2)
        with a:
            st.markdown("**Extraction**")
            st.progress(p.extraction_pct, text=f"{p.images_done}/{p.images_total} image(s) read"
                        + (f", {p.images_failed} failed" if p.images_failed else ""))
        with b:
            st.markdown("**Lookups (browser extension)**")
            st.progress(p.lookup_pct, text=f"{p.lookup_done}/{p.eligible} done · {p.failed} failed · "
                        f"{p.remaining} remaining · {p.lookup_pct:.0%}")
        st.markdown(theme.kpi_row([
            ("Rows", p.rows_total), ("Needs review", p.review), ("Success", p.success),
            ("Not found", p.not_found), ("Failed", p.failed), ("Gemini cost", f"${p.cost_usd:.4f}"),
        ]), unsafe_allow_html=True)
        for img in db.get_images(job_id):
            if img["status"] == "EXTRACTION_FAILED":
                st.error(f"{img['filename']}: {img['error']}")
        screens = db.get_pnr_screens(job_id)
        if screens:
            st.markdown("**GDS PNR screens**")
            st.dataframe(pd.DataFrame([{
                "Screenshot": s["filename"], "Status": s["status"], "PNR(s) found": s["pnrs"] or "",
                "Rows filled": s["matched_rows"],
                "Not matched": ", ".join(unmatched_list(s)), "Note": s["error"] or "",
            } for s in screens]), hide_index=True, width="stretch")
        rows = db.get_rows(job_id)
        if rows:
            df = _rows_frame(rows)
            st.dataframe(_style_rows(df), hide_index=True, width="stretch",
                         height=min(38 + 35 * len(df), 560))
        elif active:
            st.info("Waiting for the worker to read the images…")
        if active:
            st.caption("Updates every 3 seconds.")

    live()


# ---------------------------------------------------------------- review
def _open_for_lookups(job_id: int) -> None:
    try:
        jobs.start_lookups(db, job_id)
    except jobs.JobActionError as exc:  # e.g. not enough FLT credits
        flash("error", str(exc))
        st.rerun()
    goto("Job details")


def page_review(job_id: int) -> None:
    job = db.get_job(job_id)
    st.title(f"Review · Job #{job_id}")
    rows = [r for r in db.get_rows(job_id) if r["extraction_status"] == ExtractionStatus.NEEDS_REVIEW]
    if not rows:
        st.success("Nothing to review in this job.")
        if job["status"] in jobs.LOOKUP_STARTABLE and not job["lookup_requested"] and \
                st.button("▶ Open for lookups", type="primary"):
            _open_for_lookups(job_id)
        return

    st.caption("Check each flagged row against the photo. Hover over the photo and click ⤢ to enlarge it. "
               "Fix the surname or PNR if needed, then Approve, or Reject to leave the row out.")
    a, b, _ = st.columns([1, 1.4, 2])
    if a.button("✔ Approve all valid", help="Approve every flagged row whose surname and PNR already "
                                            "pass validation (e.g. flagged only for low confidence)"):
        n = jobs.approve_all_valid(db, job_id)
        flash("success", f"Approved {n} row(s).")
        st.rerun()
    if job["status"] in jobs.LOOKUP_STARTABLE and not job["lookup_requested"] and \
            b.button(f"▶ Open for lookups, skip {len(rows)} unreviewed"):
        _open_for_lookups(job_id)

    images = {i["id"]: i for i in db.get_images(job_id)}
    by_image: dict[int, list] = {}
    for r in rows:
        by_image.setdefault(r["image_id"], []).append(r)

    for image_id, img_rows in by_image.items():
        img = images[image_id]
        st.divider()
        left, right = st.columns([1.3, 1])
        with left:
            try:
                st.image(storage.read_bytes(img["stored_path"]), caption=img["filename"], width="stretch")
            except (OSError, StorageError):
                st.warning(f"{img['filename']}: image file no longer available")
        with right:
            for r in img_rows:
                _review_form(r)


def _conf(v) -> str:
    return "–" if v is None else f"{v:.2f}"


def _review_form(r) -> None:
    with st.container(border=True):
        st.markdown(f"**Line {r['line_no'] or '?'}** · surname conf {_conf(r['surname_confidence'])} · "
                    f"PNR conf {_conf(r['pnr_confidence'])}")
        if r["notes"]:
            st.caption(r["notes"])
        st.caption(f"AI read: surname `{r['surname'] or '–'}` · first `{r['first_name'] or '–'}` · "
                   f"PNR `{r['pnr'] or '–'}`")
        with st.form(f"review_{r['id']}"):
            c1, c2, c3 = st.columns([1.3, 1, 0.8])
            surname = c1.text_input("Surname", r["effective_surname"] or "")
            first = c2.text_input("First name", r["effective_first_name"] or "")
            pnr = c3.text_input("PNR", r["effective_pnr"] or "", max_chars=6)
            b1, b2 = st.columns(2)
            approve = b1.form_submit_button("✔ Approve", type="primary", width="stretch")
            reject = b2.form_submit_button("✖ Reject", width="stretch")
        if approve:
            errors = jobs.approve_row(db, r["id"], surname=surname, first_name=first, pnr=pnr)
            if errors:
                for e in errors:
                    st.error(e)
            else:
                flash("success", f"Line {r['line_no']} approved.")
                st.rerun()
        if reject:
            jobs.reject_row(db, r["id"])
            flash("info", f"Line {r['line_no']} rejected (won't be looked up).")
            st.rerun()


# --------------------------------------------------------------- results
@st.cache_data(show_spinner=False, max_entries=8)
def _final_files(job_id: int, fingerprint: str) -> tuple[bytes, bytes]:
    xlsx_key, csv_key = jobs.export_final(db, storage, job_id)
    return storage.read_bytes(xlsx_key), storage.read_bytes(csv_key)


def page_results(job_id: int) -> None:
    job = db.get_job(job_id)
    st.title(f"Results · Job #{job_id}: {job['name']}")
    st.markdown(badge(job["status"]))
    headers, table = jobs.build_final(db, job_id)
    if not table:
        st.info("No rows yet.")
        return

    fp = db.conn.execute("SELECT COUNT(*) || MAX(updated_at) FROM rows WHERE job_id=?", (job_id,)).fetchone()[0]
    xlsx, csv = _final_files(job_id, f"{fp}|{job['status']}|{job['updated_at']}")
    d = st.columns(4)
    d[0].download_button("⬇️ Final Excel", xlsx, file_name=f"job_{job_id}_final.xlsx", type="primary",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         width="stretch")
    d[1].download_button("⬇️ Final CSV", csv, file_name=f"job_{job_id}_final.csv", mime="text/csv",
                         width="stretch")
    ex1 = storage.output_key(job_id, f"job_{job_id}_extracted.xlsx")
    if storage.exists(ex1):
        d[2].download_button("⬇️ Excel #1 (extracted)", storage.read_bytes(ex1),
                             file_name=f"job_{job_id}_extracted.xlsx", width="stretch",
                             mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    df = pd.DataFrame(table, columns=headers)
    f1, f2, f3 = st.columns([2, 1.5, 1.5])
    q = f1.text_input("Search", placeholder="surname, PNR, flight…")
    look = f2.multiselect("Lookup status", sorted(df["Lookup Status"].dropna().unique()))
    extr = f3.multiselect("Extraction status", sorted(df["Extraction Status"].dropna().unique()))
    view = df
    if q:
        mask = view.astype(str).apply(lambda col: col.str.contains(q, case=False, regex=False)).any(axis=1)
        view = view[mask]
    if look:
        view = view[view["Lookup Status"].isin(look)]
    if extr:
        view = view[view["Extraction Status"].isin(extr)]
    st.caption(f"{len(view)} of {len(df)} rows")
    st.dataframe(view, hide_index=True, width="stretch", height=min(38 + 35 * len(view), 640))
    if len(view) != len(df):
        st.download_button("⬇️ CSV of filtered rows", table_to_csv_bytes(list(view.columns), view.values.tolist()),
                           file_name=f"job_{job_id}_filtered.csv", mime="text/csv")
    _fix_and_retry(job_id)


def _fix_and_retry(job_id: int) -> None:
    """Rows the website couldn't find (often a surname/PNR misread from the photo): fix and search again."""
    rows = [r for r in db.get_rows(job_id) if r["extraction_status"] in ("READY", "APPROVED")
            and r["lookup_status"] in jobs.FIXABLE]
    if not rows:
        return
    with st.expander(f"🔁 Fix & retry · {len(rows)} row(s) not found, mismatched or skipped", expanded=True):
        st.caption("Check the surname and PNR against the photo, correct them, and press Fix & retry. "
                   "The row goes back to the extension's queue and is searched again.")
        images = {i["id"]: i for i in db.get_images(job_id)}
        for r in rows:
            left, right = st.columns([1.3, 1])
            with left:
                img = images.get(r["image_id"])
                try:
                    st.image(storage.read_bytes(img["stored_path"]), caption=img["filename"], width="stretch")
                except (OSError, StorageError, TypeError):
                    st.caption("photo not available")
            with right, st.form(f"fix_{r['id']}"):
                st.markdown(f"**Line {r['line_no'] or '?'}** · {r['lookup_status'].replace('_', ' ')}"
                            + (f" · {r['error_message']}" if r["error_message"] else ""))
                c1, c2, c3 = st.columns([1.3, 1, 0.8])
                surname = c1.text_input("Surname", r["effective_surname"] or "")
                first = c2.text_input("First name", r["effective_first_name"] or "")
                pnr = c3.text_input("PNR", r["effective_pnr"] or "", max_chars=6)
                if st.form_submit_button("🔁 Fix & retry", type="primary", width="stretch"):
                    errors = jobs.fix_and_retry(db, r["id"], surname=surname, first_name=first, pnr=pnr)
                    for e in errors:
                        st.error(e)
                    if not errors:
                        flash("success", f"Line {r['line_no']} queued again as {pnr.strip().upper()}.")
                        st.rerun()


# ----------------------------------------------------------- FLT credits
def page_credits() -> None:
    st.title("FLT credits")
    intro("1 FLT = 1 row that got its booking details. Rows that are not found, skipped or rejected "
          "cost nothing, and a row is charged only once (retries and re-reads are free). Each account "
          "has its own FLT balance.")

    # Everyone sees their own balance.
    b = credits.balance(db, CURRENT_USER_ID)
    if not b.active:
        st.info("FLT credits are off: lookups are not limited yet. They start when the admin adds the "
                "first plan.")
    st.markdown(theme.kpi_row([
        ("Your available", b.available if b.active else "–"),
        ("On hold (open jobs)", b.held),
        ("Used", b.used),
    ], animate=True), unsafe_allow_html=True)
    st.caption(f"Balance {b.balance:,} FLT − {b.held:,} on hold for rows of jobs open for lookups = "
               f"{b.available:,} available.")

    if not IS_ADMIN:
        st.caption("Only an admin can add FLT. Ask the admin to top up your account.")
        entries = credits.ledger(db, user_id=CURRENT_USER_ID)
        _flt_history(entries, show_user=False)
        return

    # Admin: allocate FLT to any account.
    st.divider()
    account_users = users.list_users(db)
    by_label = {f"{u.email}" + (" (admin)" if u.is_admin else ""): u for u in account_users}
    with st.form("add_plan", clear_on_submit=True):
        st.markdown("**Add a plan to an account**")
        who = st.selectbox("Account", list(by_label))
        c1, c2 = st.columns([1, 1])
        choice = c1.selectbox("Plan", [*credits.PLANS, "Custom amount"])
        custom = c2.number_input("FLT (custom amount)", min_value=1, max_value=1_000_000, value=1000, step=500)
        note = st.text_input("Note (optional)", placeholder="e.g. invoice / payment reference")
        if st.form_submit_button("➕ Add plan", type="primary"):
            target = by_label[who]
            amount = credits.PLANS.get(choice, int(custom))
            credits.add_plan(db, amount, target.id, plan=choice if choice in credits.PLANS else None,
                             note=note, by_user=st.session_state.signed_in_as)
            new_bal = credits.balance(db, target.id).balance
            flash("success", f"{amount:,} FLT added to {target.email}. Their balance: {new_bal:,} FLT.")
            st.rerun()

    # Admin overview: each account's balance.
    st.markdown("**Balances by account**")
    rows = []
    for u in account_users:
        ub = credits.balance(db, u.id)
        rows.append({"Account": u.email, "Available": ub.available, "On hold": ub.held,
                     "Used": ub.used, "Balance": ub.balance})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    _flt_history(credits.ledger(db), show_user=True, user_email={u.id: u.email for u in account_users})


def _flt_history(entries, *, show_user: bool, user_email: dict | None = None) -> None:
    if not entries:
        return
    st.markdown("**History**")
    user_email = user_email or {}
    data = []
    for e in entries:
        row = {
            "When (UTC)": e["created_at"][:16].replace("T", " "),
            "Type": {"topup": "Plan added", "charge": "Used", "adjust": "Adjustment"}[e["kind"]],
            "FLT": e["amount"], "Plan": e["plan"] or "", "Job": f"#{e['job_id']}" if e["job_id"] else "",
            "Note": e["note"] or "", "By": e["by_user"] or "",
        }
        if show_user:
            row = {"Account": user_email.get(e["user_id"], "—"), **row}
        data.append(row)
    st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch")


# ------------------------------------------------------ extension tokens
def page_tokens() -> None:
    st.title("Extension tokens")
    st.caption("Each staff member's browser extension signs in with its own token. Paste it once into the "
               "extension's settings (side panel → ⚙). The token is shown only when it's created; "
               "only a fingerprint of it is stored. Revoke a token when someone leaves or a laptop is lost; "
               "PNRs they were holding go straight back to the queue.")
    st.caption(f"Extension API address: `http://{settings.api_host}:{settings.api_port}` "
               "(on the VPS: your HTTPS domain).")
    new = st.session_state.pop("_new_token", None)
    if new:
        st.success(f"Token for **{new[0]}** created. Copy it now; it won't be shown again.")
        st.code(new[1], language=None)
    with st.form("new_token", clear_on_submit=True):
        name = st.text_input("Staff member / device name", placeholder="e.g. Asha - front desk PC")
        if st.form_submit_button("Create token", type="primary"):
            try:
                st.session_state["_new_token"] = (name.strip(),
                                                  lookups.create_token(db, name, user_id=CURRENT_USER_ID))
            except lookups.LookupRequestError as exc:
                flash("error", str(exc))
            st.rerun()
    tokens = lookups.list_tokens(db, CURRENT_USER_ID)
    if not tokens:
        st.info("No tokens yet.")
        return
    for t in tokens:
        c = st.columns([2, 1.4, 1.4, 1])
        c[0].markdown(f"**{t['name']}**" + ("  · ~~revoked~~" if t["revoked_at"] else ""))
        c[1].caption(f"created {t['created_at'][:16].replace('T', ' ')}")
        c[2].caption(f"last used {(t['last_used_at'] or '–')[:16].replace('T', ' ')}")
        if not t["revoked_at"] and c[3].button("Revoke", key=f"revoke_{t['id']}"):
            lookups.revoke_token(db, t["id"])
            flash("info", f"Token '{t['name']}' revoked.")
            st.rerun()


# ------------------------------------------------------------ manage users
def page_users() -> None:
    if not IS_ADMIN:
        st.error("Admins only.")
        return
    st.title("Manage users")
    st.caption("You create every account — there is no public sign-up. Enter an email, and a strong "
               "password is generated for you to hand over. Each person can be signed in on only one "
               "device at a time; a new sign-in signs the old device out.")

    new = st.session_state.pop("_new_user", None)
    if new:
        st.success(f"Account ready for **{new[0]}**. Copy this password now — it is shown only once:")
        st.code(new[1], language=None)

    with st.form("new_user", clear_on_submit=True):
        st.markdown("**Add a user**")
        email = st.text_input("Email", placeholder="person@example.com", autocomplete="off")
        if st.form_submit_button("Create account", type="primary"):
            try:
                user, pw = users.create_user(db, email)
                st.session_state["_new_user"] = (user.email, pw)
            except users.UserError as exc:
                flash("error", str(exc))
            st.rerun()

    st.divider()
    st.markdown("**Accounts**")
    for u in users.list_users(db):
        c = st.columns([2.4, 1.3, 1.3, 1, 1])
        label = f"**{u.email}**" + ("  · _admin_" if u.is_admin else "") + ("  · ~~disabled~~" if not u.active else "")
        c[0].markdown(label)
        c[1].caption(f"created {u.created_at[:10]}")
        c[2].caption(f"last seen {(u.last_seen_at or '–')[:16].replace('T', ' ')}")
        if c[3].button("Reset password", key=f"reset_{u.id}"):
            pw = users.reset_password(db, u.id)
            st.session_state["_new_user"] = (u.email, pw)
            st.rerun()
        if not u.is_admin:
            if u.active and c[4].button("Disable", key=f"disable_{u.id}"):
                users.set_disabled(db, u.id, True)
                flash("info", f"{u.email} disabled and signed out.")
                st.rerun()
            if not u.active and c[4].button("Enable", key=f"enable_{u.id}"):
                users.set_disabled(db, u.id, False)
                flash("success", f"{u.email} enabled.")
                st.rerun()


# ---------------------------------------------------------------- router
job_id = st.session_state.get("current_job")
if page == "Dashboard":
    page_dashboard()
elif page == "New job":
    page_new_job()
elif page == "FLT credits":
    page_credits()
elif page == "Extension tokens":
    page_tokens()
elif page == "Manage users":
    page_users()
elif job_id is None:
    st.info("No jobs yet. Create one with **New job**.")
elif page == "Job details":
    page_job_details(job_id)
elif page == "Review":
    page_review(job_id)
elif page == "Results":
    page_results(job_id)
