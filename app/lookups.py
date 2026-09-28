"""Browser-extension lookups: tokens, PNR leases, captures, statuses (newplan section 5, step A).

Used by the FastAPI service (app/api.py) and the dashboard. A person triggers every airline
search in the extension; this module only hands out work and stores what the extension saw.

- A job's unique PNRs (from READY/APPROVED rows) are leased in batches to one token at a time.
  A lease lasts LEASE_MINUTES and is renewed whenever that token claims again; an expired lease
  can be taken by anyone. Rows still needing review are never handed out.
- One `lookups` row per (job, PNR). Every row with that PNR mirrors its status, so progress,
  the Summary sheet and the final Excel work unchanged. Rows already finished (e.g. filled from
  a GDS screen) are left alone.
- Capture uploads are idempotent on (job, PNR): a second upload is ignored unless `recapture`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import credits
from app.core.config import SiteProfile, Settings, list_site_profiles, load_site_profile
from app.core.db import ELIGIBLE_SQL, Database, now
from app.core.logging_setup import mask
from app.core.models import LOOKUP_DONE, JobStatus, LookupStatus
from app.core.storage import LocalStorage, sniff_image_mime
from app.jobs import progress as job_progress
from app.lookup.redact import redact_personal

log = logging.getLogger(__name__)

TOKEN_PREFIX = "gds_"
OPEN_STATUSES = (JobStatus.READY_FOR_LOOKUP, JobStatus.LOOKING_UP)
LISTED_STATUSES = (JobStatus.READY_FOR_LOOKUP, JobStatus.LOOKING_UP, JobStatus.PAUSED)
# Statuses the extension may report without a capture.
REPORTABLE = {LookupStatus.NOT_FOUND, LookupStatus.SKIPPED, LookupStatus.BLOCKED, LookupStatus.MISMATCH}
_FINISHED_SQL = "(" + ",".join(f"'{s}'" for s in sorted(LOOKUP_DONE)) + ")"


class LookupRequestError(Exception):
    """A request the API answers with 4xx. `code` is the HTTP status."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------- tokens
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(db: Database, name: str) -> str:
    """Create an extension token. Returns the token; only its hash is stored (show it once)."""
    name = (name or "").strip()
    if not name:
        raise LookupRequestError("give the token a name (e.g. the staff member's name)")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with db.tx() as c:
        c.execute("INSERT INTO api_tokens(name, token_hash, created_at) VALUES (?,?,?)",
                  (name[:80], hash_token(token), now()))
    log.info("Extension token created", extra={"token_name": name[:80]})
    return token


def list_tokens(db: Database):
    return db.conn.execute("SELECT id, name, created_at, last_used_at, revoked_at FROM api_tokens "
                           "ORDER BY revoked_at IS NOT NULL, id DESC").fetchall()


def revoke_token(db: Database, token_id: int) -> bool:
    """Revoke a token; its unfinished leases go back to the queue at once."""
    with db.write_tx() as c:
        cur = c.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now(), token_id))
        held = c.execute("SELECT job_id, pnr FROM lookups WHERE token_id=? AND status='CLAIMED'",
                         (token_id,)).fetchall()
        c.execute("UPDATE lookups SET status='NOT_STARTED', token_id=NULL, lease_until=NULL, updated_at=? "
                  "WHERE token_id=? AND status='CLAIMED'", (now(), token_id))
        for r in held:
            _mirror(c, r["job_id"], r["pnr"])
    return cur.rowcount == 1


def authenticate(db: Database, token: str | None):
    """The api_tokens row for a valid, unrevoked token, else None."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = db.conn.execute("SELECT * FROM api_tokens WHERE token_hash=?", (hash_token(token),)).fetchone()
    if row is None or row["revoked_at"]:
        return None
    last = row["last_used_at"]
    if not last or last < _ts(_utcnow() - timedelta(minutes=1)):  # don't write on every request
        with db.tx() as c:
            c.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now(), row["id"]))
    return row


# --------------------------------------------------------------------- jobs
def open_jobs(db: Database) -> list[dict]:
    """Jobs the extension can work on (opened for lookups on the dashboard)."""
    out = []
    for j in db.list_jobs():
        if j["status"] in LISTED_STATUSES and j["lookup_requested"]:
            p = job_progress(db, j["id"])
            out.append({"id": j["id"], "name": j["name"], "status": j["status"],
                        "remaining": p.remaining, "eligible": p.eligible, "error": j["error_message"]})
    return out


def _job_open_for_claims(db: Database, job_id: int):
    job = db.get_job(job_id)
    if job is None:
        raise LookupRequestError("job not found", 404)
    if job["status"] == JobStatus.PAUSED:
        raise LookupRequestError(f"job is paused: {job['error_message'] or 'resume it on the dashboard'}", 409)
    if job["status"] not in OPEN_STATUSES or not job["lookup_requested"]:
        raise LookupRequestError(f"job is {job['status']}; open it for lookups on the dashboard first", 409)
    return job


# ------------------------------------------------------------------- claims
def _mirror(c, job_id: int, pnr: str) -> None:
    """Copy the PNR lookup's status to every eligible, not yet finished row with that PNR."""
    c.execute(
        f"""UPDATE rows SET lookup_status=(SELECT status FROM lookups WHERE job_id=? AND pnr=?),
                error_message=(SELECT note FROM lookups WHERE job_id=? AND pnr=?),
                lookup_started_at=COALESCE(lookup_started_at, ?), updated_at=?
            WHERE job_id=? AND COALESCE(edited_pnr, pnr)=? AND extraction_status IN {ELIGIBLE_SQL}
            AND lookup_status NOT IN {_FINISHED_SQL}""",
        (job_id, pnr, job_id, pnr, now(), now(), job_id, pnr))


def _items(db: Database, job_id: int, leases) -> list[dict]:
    """Lease rows -> what the extension shows: PNR, surname to type, every passenger on it."""
    pax: dict[str, list[dict]] = {}
    for r in db.get_rows(job_id):
        if r["extraction_status"] in ("READY", "APPROVED") and r["effective_pnr"]:
            pax.setdefault(r["effective_pnr"], []).append(
                {"surname": r["effective_surname"], "first_name": r["effective_first_name"],
                 "title": r["title"], "line_no": r["line_no"]})
    return [{"pnr": x["pnr"], "surname": (pax.get(x["pnr"]) or [{}])[0].get("surname"),
             "passengers": pax.get(x["pnr"], []), "lease_until": x["lease_until"]} for x in leases]


def claim(db: Database, job_id: int, token, n: int, settings: Settings) -> list[dict]:
    """Lease up to `n` PNRs to `token` (renewing the ones it already holds), in photo order.

    Returns the token's current leases, oldest first: [{pnr, surname, passengers, lease_until}]."""
    n = max(1, min(int(n), 100))
    _job_open_for_claims(db, job_id)
    if not credits.claims_allowed(db):
        raise LookupRequestError("Not enough FLT credits: add a plan on the dashboard (FLT credits page)", 402)
    ts = _utcnow()
    lease_until = _ts(ts + timedelta(minutes=settings.lease_minutes))
    with db.write_tx() as c:
        c.execute("UPDATE lookups SET lease_until=?, updated_at=? WHERE job_id=? AND token_id=? AND status='CLAIMED'",
                  (lease_until, now(), job_id, token["id"]))
        held = c.execute("SELECT COUNT(*) FROM lookups WHERE job_id=? AND token_id=? AND status='CLAIMED'",
                         (job_id, token["id"])).fetchone()[0]
        want = n - held
        if want > 0:
            # PNRs that still have an unfinished eligible row and aren't leased to someone else.
            free = c.execute(
                f"""SELECT COALESCE(r.edited_pnr, r.pnr) AS p, MIN(r.image_id * 100000 + r.seq) AS ord
                    FROM rows r LEFT JOIN lookups l ON l.job_id=r.job_id AND l.pnr=COALESCE(r.edited_pnr, r.pnr)
                    WHERE r.job_id=? AND r.extraction_status IN {ELIGIBLE_SQL}
                      AND COALESCE(r.edited_pnr, r.pnr) IS NOT NULL
                      AND r.lookup_status IN ('NOT_STARTED','CLAIMED')
                      AND (l.status IS NULL OR l.status='NOT_STARTED'
                           OR (l.status='CLAIMED' AND l.lease_until < ?))
                    GROUP BY p ORDER BY ord LIMIT ?""",
                (job_id, _ts(ts), want)).fetchall()
            for r in free:
                c.execute(
                    """INSERT INTO lookups(job_id, pnr, status, token_id, looked_up_by, lease_until, claimed_at,
                           updated_at) VALUES (?,?,'CLAIMED',?,?,?,?,?)
                       ON CONFLICT(job_id, pnr) DO UPDATE SET status='CLAIMED', token_id=excluded.token_id,
                           looked_up_by=excluded.looked_up_by, lease_until=excluded.lease_until,
                           claimed_at=excluded.claimed_at, updated_at=excluded.updated_at""",
                    (job_id, r["p"], token["id"], token["name"], lease_until, now(), now()))
                _mirror(c, job_id, r["p"])
        c.execute("UPDATE jobs SET status='LOOKING_UP', updated_at=? WHERE id=? AND status='READY_FOR_LOOKUP'",
                  (now(), job_id))
        mine = c.execute("SELECT pnr, lease_until FROM lookups WHERE job_id=? AND token_id=? AND status='CLAIMED' "
                         "ORDER BY claimed_at, rowid", (job_id, token["id"])).fetchall()
    return _items(db, job_id, mine)


def release(db: Database, job_id: int, token, pnrs: list[str] | None = None) -> int:
    """Give back this token's unfinished leases (all, or just `pnrs`)."""
    sql = "UPDATE lookups SET status='NOT_STARTED', token_id=NULL, lease_until=NULL, updated_at=? " \
          "WHERE job_id=? AND token_id=? AND status='CLAIMED'"
    args: list = [now(), job_id, token["id"]]
    if pnrs:
        sql += f" AND pnr IN ({','.join('?' * len(pnrs))})"
        args += [p.upper() for p in pnrs]
    with db.write_tx() as c:
        released = [r["pnr"] for r in c.execute(
            "SELECT pnr FROM lookups WHERE job_id=? AND token_id=? AND status='CLAIMED'", (job_id, token["id"]))
            if not pnrs or r["pnr"] in {p.upper() for p in pnrs}]
        c.execute(sql, args)
        for p in released:
            _mirror(c, job_id, p)
    return len(released)


def _lookup_for_update(c, job_id: int, pnr: str, token):
    row = c.execute("SELECT * FROM lookups WHERE job_id=? AND pnr=?", (job_id, pnr)).fetchone()
    if row is None:
        eligible = c.execute(f"SELECT 1 FROM rows WHERE job_id=? AND COALESCE(edited_pnr, pnr)=? "
                             f"AND extraction_status IN {ELIGIBLE_SQL} LIMIT 1", (job_id, pnr)).fetchone()
        if not eligible:
            raise LookupRequestError(f"PNR {pnr} is not part of job {job_id} (or still needs review)", 404)
        c.execute("INSERT INTO lookups(job_id, pnr, status, updated_at) VALUES (?,?,'NOT_STARTED',?)",
                  (job_id, pnr, now()))
        row = c.execute("SELECT * FROM lookups WHERE job_id=? AND pnr=?", (job_id, pnr)).fetchone()
    held_by_other = (row["status"] == LookupStatus.CLAIMED and row["token_id"] not in (None, token["id"])
                     and (row["lease_until"] or "") >= now())
    if held_by_other:
        raise LookupRequestError(f"PNR {pnr} is being looked up by someone else", 409)
    return row


# ------------------------------------------------------------------ capture
@dataclass
class CaptureResult:
    status: str
    stored: bool  # False when an earlier capture was kept (idempotent repeat)


def save_capture(db: Database, storage: LocalStorage, settings: Settings, job_id: int, pnr: str, token, *,
                 text: str | None, screenshot_b64: str | None, url: str | None,
                 recapture: bool = False) -> CaptureResult:
    """Store the result page the extension captured. Idempotent on (job, PNR)."""
    pnr = pnr.upper()
    if db.get_job(job_id) is None:
        raise LookupRequestError("job not found", 404)
    text = (text or "").strip()
    if len(text.encode("utf-8")) > settings.max_capture_text_kb * 1024:
        raise LookupRequestError("captured text is too large", 413)
    shot: bytes | None = None
    if screenshot_b64:
        try:
            shot = base64.b64decode(screenshot_b64.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError):
            raise LookupRequestError("screenshot is not valid base64") from None
        if len(shot) > settings.max_capture_screenshot_mb * 1024 * 1024:
            raise LookupRequestError("screenshot is too large", 413)
        if sniff_image_mime(shot) not in ("image/png", "image/jpeg"):
            raise LookupRequestError("screenshot must be PNG or JPEG")
    if not text and not shot:
        raise LookupRequestError("capture has neither page text nor a screenshot")

    with db.write_tx() as c:
        row = _lookup_for_update(c, job_id, pnr, token)
        if row["status"] in (LookupStatus.CAPTURED, LookupStatus.PARSED) and not recapture:
            return CaptureResult(row["status"], False)
        text_key = f"pages/job_{int(job_id)}/{pnr}.txt"
        shot_key = f"pages/job_{int(job_id)}/{pnr}.png" if shot else None
        storage.write_bytes(text_key, redact_personal(text).encode("utf-8"))  # no passport / birth data
        if shot:
            storage.write_bytes(shot_key, shot)
        c.execute(
            """UPDATE lookups SET status='CAPTURED', token_id=?, looked_up_by=?, lease_until=NULL,
                   page_text_path=?, screenshot_path=?, page_url=?, note=NULL, captures=captures+1,
                   captured_at=?, finished_at=NULL, updated_at=? WHERE job_id=? AND pnr=?""",
            (token["id"], token["name"], text_key, shot_key, (url or "")[:500], now(), now(), job_id, pnr))
        if recapture:  # allow finished rows of this PNR to be parsed again
            c.execute(f"UPDATE rows SET lookup_status='NOT_STARTED' WHERE job_id=? AND COALESCE(edited_pnr, pnr)=? "
                      f"AND extraction_status IN {ELIGIBLE_SQL}", (job_id, pnr))
        _mirror(c, job_id, pnr)
    log.info("Capture stored", extra={"job_id": job_id, "pnr": mask(pnr), "text_chars": len(text),
                                      "screenshot": bool(shot), "recapture": recapture})
    return CaptureResult(LookupStatus.CAPTURED, True)


def set_status(db: Database, job_id: int, pnr: str, token, status: str, note: str | None = None) -> str:
    """NOT_FOUND / SKIPPED / BLOCKED / MISMATCH reported by the extension. BLOCKED pauses the job."""
    pnr = pnr.upper()
    try:
        st = LookupStatus(status)
    except ValueError:
        raise LookupRequestError(f"unknown status {status!r}") from None
    if st not in REPORTABLE:
        raise LookupRequestError(f"status must be one of {sorted(REPORTABLE)}")
    if db.get_job(job_id) is None:
        raise LookupRequestError("job not found", 404)
    note = (note or "").strip()[:300] or {
        LookupStatus.NOT_FOUND: "booking not found on the website",
        LookupStatus.SKIPPED: "skipped by staff",
        LookupStatus.BLOCKED: "website showed a CAPTCHA / access denied page",
        LookupStatus.MISMATCH: "result page showed another booking",
    }[st]
    with db.write_tx() as c:
        row = _lookup_for_update(c, job_id, pnr, token)
        if row["status"] in (LookupStatus.CAPTURED, LookupStatus.PARSED) and st != LookupStatus.MISMATCH:
            return row["status"]  # a late duplicate report doesn't undo a capture
        c.execute("""UPDATE lookups SET status=?, token_id=?, looked_up_by=?, lease_until=NULL, note=?,
                         finished_at=?, updated_at=? WHERE job_id=? AND pnr=?""",
                  (st.value, token["id"], token["name"], note, now(), now(), job_id, pnr))
        _mirror(c, job_id, pnr)
        if st == LookupStatus.BLOCKED:
            c.execute("UPDATE jobs SET status='PAUSED', error_message=?, updated_at=? "
                      "WHERE id=? AND status IN ('READY_FOR_LOOKUP','LOOKING_UP')",
                      ("paused: the website showed a CAPTCHA / access denied page. Handle it in the browser, "
                       "then click Resume.", now(), job_id))
    log.info("Lookup status reported", extra={"job_id": job_id, "pnr": mask(pnr), "status": st.value})
    return st.value


def resume(db: Database, job_id: int) -> bool:
    """Re-open a paused job; BLOCKED PNRs go back to the queue (same as Resume on the dashboard)."""
    from app.jobs import resume_job
    if db.get_job(job_id) is None:
        raise LookupRequestError("job not found", 404)
    return resume_job(db, job_id)


# ----------------------------------------------------------------- progress
def progress(db: Database, job_id: int) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise LookupRequestError("job not found", 404)
    p = job_progress(db, job_id)
    since = _ts(_utcnow() - timedelta(minutes=10))
    counts = {r["status"]: r["n"] for r in db.conn.execute(
        "SELECT status, COUNT(*) AS n FROM lookups WHERE job_id=? GROUP BY status", (job_id,))}
    recent = db.conn.execute(
        "SELECT MIN(COALESCE(captured_at, finished_at)) AS first, COUNT(*) AS n FROM lookups WHERE job_id=? "
        "AND COALESCE(captured_at, finished_at) >= ?", (job_id, since)).fetchone()
    per_min = 0.0
    if recent["n"]:
        span = max(60.0, (_utcnow() - datetime.fromisoformat(recent["first"])).total_seconds())
        per_min = recent["n"] / (span / 60)
    pnrs_left = db.conn.execute(
        f"""SELECT COUNT(DISTINCT COALESCE(edited_pnr, pnr)) FROM rows WHERE job_id=?
            AND extraction_status IN {ELIGIBLE_SQL} AND lookup_status IN ('NOT_STARTED','CLAIMED')""",
        (job_id,)).fetchone()[0]
    return {
        "job_id": job_id, "status": job["status"], "message": job["error_message"],
        "rows": {"eligible": p.eligible, "done": p.lookup_done, "parsed": p.success, "not_found": p.not_found,
                 "failed": p.failed, "in_progress": p.in_progress, "remaining": p.remaining},
        "pnrs": {"left": pnrs_left, **{s.value.lower(): counts.get(s.value, 0) for s in LookupStatus}},
        "per_minute": round(per_min, 1),
        "eta_minutes": round(pnrs_left / per_min, 1) if per_min else None,
        "cost_usd": round(p.cost_usd, 4),
    }


# ------------------------------------------------------------ site profiles
def get_site_profile(db: Database, name: str) -> SiteProfile:
    """The DB copy (saved from the picker/dashboard) wins over config/websites/<name>.yaml."""
    row = db.conn.execute("SELECT profile_json FROM site_profiles WHERE name=?", (name,)).fetchone()
    if row:
        return SiteProfile.model_validate_json(row["profile_json"])
    files = list_site_profiles()
    if name not in files:
        raise LookupRequestError(f"site profile {name!r} not found", 404)
    return load_site_profile(files[name])


def site_profile_names(db: Database) -> list[str]:
    names = set(list_site_profiles()) | {r["name"] for r in db.conn.execute("SELECT name FROM site_profiles")}
    return sorted(names)


def save_site_profile(db: Database, name: str, profile: SiteProfile) -> None:
    if not name.replace("_", "").replace("-", "").isalnum():
        raise LookupRequestError("profile name may contain only letters, digits, - and _")
    with db.tx() as c:
        c.execute("""INSERT INTO site_profiles(name, profile_json, updated_at) VALUES (?,?,?)
                     ON CONFLICT(name) DO UPDATE SET profile_json=excluded.profile_json,
                     updated_at=excluded.updated_at""", (name, profile.model_dump_json(), now()))
    log.info("Site profile saved", extra={"profile": name})

