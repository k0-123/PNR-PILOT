"""AlFursan signup batches: an uploaded passenger Excel, signed up one person after another.

Every person keeps their Excel row (JSON). The extension asks for the next person, fills the
Register form from that person's own columns (missing columns stay empty for staff to type), staff
press Confirm, then Next. Email = firstname@domain (signups.unique_email). The worker's mail poll
(signups.poll_mail) activates and captures the membership number; `batch_excel` writes the Excel
back with Email / AlFursan No. / Signup Status columns.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from app.core.config import Settings
from app.core.db import Database, now
from app.signups import REGISTER_URL, unique_email

# form field -> Excel header aliases (compared lowercased, letters only). Exact match first.
FIELD_HEADERS: list[tuple[str, tuple[str, ...]]] = [
    ("title", ("title", "salutation", "prefix")),
    ("first_name", ("firstname", "givenname", "givennames", "first", "name")),
    ("middle_name", ("middlename", "middle")),
    ("last_name", ("lastname", "surname", "familyname", "last")),
    ("gender", ("gender", "sex")),
    ("nationality", ("nationality", "citizenship")),
    ("country", ("country", "countryofresidence", "residence")),
    ("dob", ("dob", "dateofbirth", "birthdate", "birthday")),
    ("id_number", ("passport", "passportno", "passportnumber", "idnumber", "id", "nationalid", "iqama",
                   "documentnumber")),
    ("phone", ("phone", "mobile", "mobileno", "phoneno", "phonenumber", "contactno")),
    ("city", ("city",)),
]


class BatchError(ValueError):
    pass


def _norm(h) -> str:
    return re.sub(r"[^a-z]", "", str(h or "").lower())


def map_headers(headers: list) -> dict[str, int]:
    normed = [_norm(h) for h in headers]
    out: dict[str, int] = {}
    used: set[int] = set()
    for exact in (True, False):
        for field, aliases in FIELD_HEADERS:
            if field in out:
                continue
            for i, h in enumerate(normed):
                if i in used or not h:
                    continue
                if any(h == a if exact else (len(a) > 3 and a in h) for a in aliases):
                    out[field] = i
                    used.add(i)
                    break
    return out


def _cell(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def create_batch(db: Database, settings: Settings, user_id: int | None, name: str, xlsx: bytes) -> dict:
    from openpyxl import load_workbook

    try:
        ws = load_workbook(BytesIO(xlsx), read_only=True, data_only=True).worksheets[0]
    except Exception as exc:  # noqa: BLE001 - any unreadable upload
        raise BatchError(f"Can't read the Excel file: {exc.__class__.__name__}") from None
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    hi = next((i for i, r in enumerate(rows[:10]) if len(map_headers(r)) >= 2), None)
    if hi is None:
        raise BatchError("No header row found (need columns like First name, Last name)")
    headers = [_cell(h) or f"Column {i + 1}" for i, h in enumerate(rows[hi])]
    cols = map_headers(rows[hi])
    if "first_name" not in cols:
        raise BatchError("The Excel needs a First name column")
    with db.write_tx() as c:
        batch_id = c.execute("INSERT INTO signup_batches(user_id, name, headers, created_at) VALUES (?,?,?,?)",
                             (user_id, name[:200] or "Excel", json.dumps(headers), now())).lastrowid
    n = 0
    for r in rows[hi + 1:]:
        vals = ([_cell(v) for v in r] + [""] * len(headers))[:len(headers)]
        if not any(vals):
            continue
        n += 1
        fields = {f: vals[i] for f, i in cols.items() if vals[i]}
        email = unique_email(db, fields.get("first_name") or fields.get("last_name"), settings.signup_email_domain)
        with db.write_tx() as c:
            c.execute("""INSERT INTO batch_people(batch_id, n, data_json, email, updated_at) VALUES (?,?,?,?,?)""",
                      (batch_id, n, json.dumps({"fields": fields, "raw": vals}), email, now()))
    return {"batch_id": batch_id, "people": n, "columns": {f: headers[i] for f, i in cols.items()}}


GREEN = ("PARSED",)  # final Excel's green rows: the booking was found and read


def create_batch_from_job(db: Database, settings: Settings, user_id: int | None, job_id: int) -> dict:
    """One person per green row of the job (same count as the green rows in the job's Excel)."""
    job = db.get_job(job_id)
    if job is None or job["user_id"] != user_id:
        raise KeyError(job_id)
    headers = ["Row", "Surname", "First Name", "Title", "PNR / E-ticket"]
    green = [(i, r) for i, r in enumerate(db.get_rows(job_id), start=1)
             if r["lookup_status"] in GREEN and r["extraction_status"] in ("READY", "APPROVED")]
    if not green:
        raise BatchError("This job has no green (found) rows yet")
    with db.write_tx() as c:
        batch_id = c.execute("INSERT INTO signup_batches(user_id, name, headers, created_at) VALUES (?,?,?,?)",
                             (user_id, f"Job #{job_id} {job['name']}"[:200], json.dumps(headers), now())).lastrowid
    for n, (pos, r) in enumerate(green, start=1):
        first, last = r["effective_first_name"] or "", r["effective_surname"] or ""
        fields = {k: v for k, v in {"title": (r["title"] or "").upper(), "first_name": first,
                                     "last_name": last}.items() if v}
        raw = [pos, last, first, r["title"], r["effective_pnr"]]
        email = unique_email(db, first or last, settings.signup_email_domain)
        with db.write_tx() as c:
            c.execute("""INSERT INTO batch_people(batch_id, n, row_id, data_json, email, updated_at)
                         VALUES (?,?,?,?,?,?)""",
                      (batch_id, n, r["id"], json.dumps({"fields": fields, "raw": raw}), email, now()))
    return {"batch_id": batch_id, "people": len(green), "columns": {}}


def own_batch(db: Database, batch_id: int, user_id: int | None):
    b = db.conn.execute("SELECT * FROM signup_batches WHERE id=? AND user_id IS ?", (batch_id, user_id)).fetchone()
    if b is None:
        raise KeyError(batch_id)
    return b


def list_batches(db: Database, user_id: int | None) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        """SELECT b.id, b.name, COUNT(p.id) AS people, COALESCE(SUM(p.status='PENDING'), 0) AS pending,
                  COALESCE(SUM(p.status='CAPTURED'), 0) AS captured
           FROM signup_batches b LEFT JOIN batch_people p ON p.batch_id = b.id
           WHERE b.user_id IS ? GROUP BY b.id ORDER BY b.id DESC LIMIT 50""", (user_id,))]


def _person_out(p) -> dict:
    return {"person_id": p["id"], "n": p["n"], "email": p["email"], "status": p["status"],
            **json.loads(p["data_json"])["fields"], "register_url": REGISTER_URL}


STALE_FILLED_MINUTES = 15  # a phone that crashed/closed mid-person gives the person back after this


def next_person(db: Database, batch_id: int, user_id: int | None) -> dict | None:
    """The next PENDING person, marked FILLED (the app/extension fills the form now).

    Several phones can work one batch at once: the write transaction hands each person to one caller.
    A person left FILLED for STALE_FILLED_MINUTES (app closed without Stop) is handed out again.
    """
    own_batch(db, batch_id, user_id)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=STALE_FILLED_MINUTES)).isoformat(timespec="seconds")
    with db.write_tx() as c:
        p = c.execute("""SELECT id FROM batch_people WHERE batch_id=?
                           AND (status='PENDING' OR (status='FILLED' AND updated_at < ?))
                         ORDER BY n LIMIT 1""", (batch_id, stale)).fetchone()
        if p is None:
            return None
        c.execute("UPDATE batch_people SET status='FILLED', updated_at=? WHERE id=?", (now(), p["id"]))
    return _person_out(db.conn.execute("SELECT * FROM batch_people WHERE id=?", (p["id"],)).fetchone())


def set_person_status(db: Database, person_id: int, user_id: int | None, status: str) -> bool:
    """Staff: SUBMITTED (pressed Confirm), SKIPPED, or PENDING (do again later)."""
    if status not in ("SUBMITTED", "SKIPPED", "PENDING"):
        raise BatchError("status must be SUBMITTED, SKIPPED or PENDING")
    with db.write_tx() as c:
        cur = c.execute(
            """UPDATE batch_people SET status=?, updated_at=?
               WHERE id=? AND status IN ('FILLED','PENDING','SKIPPED')
                 AND batch_id IN (SELECT id FROM signup_batches WHERE user_id IS ?)""",
            (status, now(), person_id, user_id))
    return cur.rowcount > 0


def progress(db: Database, batch_id: int, user_id: int | None) -> dict:
    own_batch(db, batch_id, user_id)
    counts = {s: n for s, n in db.conn.execute(
        "SELECT status, COUNT(*) FROM batch_people WHERE batch_id=? GROUP BY status", (batch_id,))}
    return {"counts": counts, "total": sum(counts.values())}


def batch_excel(db: Database, batch_id: int, user_id: int | None, path: Path) -> Path:
    from app.excel.writer import Sheet, write_workbook

    b = own_batch(db, batch_id, user_id)
    headers = json.loads(b["headers"])
    rows = [[*json.loads(p["data_json"])["raw"], p["email"], p["membership_no"], p["status"], p["note"]]
            for p in db.conn.execute("SELECT * FROM batch_people WHERE batch_id=? ORDER BY n", (batch_id,))]
    return write_workbook(path, [Sheet("AlFursan", [*headers, "Email", "AlFursan No.", "Signup Status", "Note"],
                                       rows, status_column="Signup Status")])
