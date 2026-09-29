"""FLT credits: a prepaid balance for lookups.

1 FLT = 1 row that got its result (website page or GDS screen). Rows that end NOT_FOUND,
SKIPPED, MISMATCH, REJECTED or DUPLICATE cost nothing, and a row is charged at most once (retries,
Fix & retry and re-reads are free).

    balance   = sum of the ledger (top-ups +, charges -)
    held      = unfinished, uncharged rows of the jobs open for lookups
    available = balance - held

Held FLT is computed, not stored: a row that finishes without a result stops being held, so its
FLT is released automatically. Opening a job for lookups (and retrying rows, fixing a row or
adding GDS screens) needs enough available FLT for the job's open rows, so the balance never goes
below zero in the middle of a job.

FLT is active once the first plan is added. Before that nothing is limited or charged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.core.db import ELIGIBLE_SQL, Database, now

log = logging.getLogger(__name__)

PLANS: dict[str, int] = {"Plan 10k": 10_000}
LOW_SHARE = 0.20  # warn at 20% of the last plan
VERY_LOW = 500    # and again at 500 FLT

# Jobs whose open rows are on hold (they are, or can be, looked up now).
_OPEN_JOBS_SQL = "SELECT id FROM jobs WHERE lookup_requested=1 AND status IN ('READY_FOR_LOOKUP','LOOKING_UP','PAUSED')"
# Rows that may still get a result and aren't charged yet.
_OPEN_ROWS_SQL = (f"extraction_status IN {ELIGIBLE_SQL} AND flt_charged_at IS NULL "
                  "AND lookup_status IN ('NOT_STARTED','CLAIMED','CAPTURED')")


class NotEnoughFLT(Exception):
    def __init__(self, needed: int, available: int):
        self.needed, self.available = needed, available
        super().__init__(f"Needs {needed:,} FLT, you have {max(available, 0):,} available: add a plan on the "
                         "FLT credits page")


@dataclass
class Balance:
    active: bool
    balance: int
    held: int
    used: int          # charged in total
    last_plan: int     # size of the most recent top-up (for the low-balance warning)

    @property
    def available(self) -> int:
        return self.balance - self.held

    @property
    def warning(self) -> str | None:
        if not self.active:
            return None
        if self.available <= 0:
            return "FLT credits are used up: add a plan to continue lookups."
        if self.available <= VERY_LOW or self.available <= self.last_plan * LOW_SHARE:
            return f"FLT credits are low: {self.available:,} left."
        return None


_ALL_USERS = object()  # ledger(): every user, not one (distinct from None = the unowned pool)


def _user_sql(user_id: int | None) -> tuple[str, tuple]:
    """SQL predicate + args for a user_id column (None means the legacy unowned pool)."""
    return ("user_id IS NULL", ()) if user_id is None else ("user_id=?", (user_id,))


def _job_owner(db: Database, job_id: int) -> int | None:
    row = db.get_job(job_id)
    return row["user_id"] if row else None


def is_active(db: Database) -> bool:
    """FLT enforcement is global: once the admin adds any plan to anyone, every user's lookups are
    limited by their own balance (a user with no FLT is blocked until the admin allocates some)."""
    return db.conn.execute("SELECT 1 FROM flt_ledger WHERE kind='topup' LIMIT 1").fetchone() is not None


def held(db: Database, user_id: int | None, exclude_job: int | None = None) -> int:
    """Uncharged open rows of this user's jobs (their FLT currently on hold)."""
    pred, uargs = _user_sql(user_id)
    sql = (f"SELECT COUNT(*) FROM rows WHERE job_id IN "
           f"(SELECT id FROM jobs WHERE lookup_requested=1 "
           f"AND status IN ('READY_FOR_LOOKUP','LOOKING_UP','PAUSED') AND {pred}) AND {_OPEN_ROWS_SQL}")
    args: tuple = uargs
    if exclude_job is not None:
        sql += " AND job_id<>?"
        args = args + (exclude_job,)
    return db.conn.execute(sql, args).fetchone()[0]


def balance(db: Database, user_id: int | None) -> Balance:
    """One user's FLT balance (None = the legacy unowned pool)."""
    pred, args = _user_sql(user_id)
    total, used = db.conn.execute(
        f"SELECT COALESCE(SUM(amount),0), COALESCE(-SUM(CASE WHEN kind='charge' THEN amount END),0) "
        f"FROM flt_ledger WHERE {pred}", args
    ).fetchone()
    last = db.conn.execute(f"SELECT amount FROM flt_ledger WHERE kind='topup' AND {pred} "
                           "ORDER BY id DESC LIMIT 1", args).fetchone()
    return Balance(active=is_active(db), balance=total, held=held(db, user_id), used=used,
                   last_plan=last[0] if last else 0)


def needed_for_job(db: Database, job_id: int) -> int:
    """FLT a job still needs: its rows that may get a result and aren't charged yet."""
    return db.conn.execute(f"SELECT COUNT(*) FROM rows WHERE job_id=? AND {_OPEN_ROWS_SQL}", (job_id,)).fetchone()[0]


def ensure_affordable(db: Database, job_id: int, extra: int = 0) -> None:
    """Raise NotEnoughFLT unless the job owner's balance covers this job's open rows (+ `extra` rows
    about to be re-opened) on top of what their other open jobs hold. No-op while FLT isn't active."""
    if not is_active(db):
        return
    owner = _job_owner(db, job_id)
    b = balance(db, owner)
    available = b.balance - held(db, owner, exclude_job=job_id)
    needed = needed_for_job(db, job_id) + extra
    if needed > available:
        raise NotEnoughFLT(needed, available)


def claims_allowed(db: Database, user_id: int | None) -> bool:
    """The extension may take more bookings only while this user's balance covers what they hold."""
    return not is_active(db) or balance(db, user_id).available >= 0


def add_plan(db: Database, amount: int, user_id: int | None, *, plan: str | None = None,
             note: str | None = None, by_user: str | None = None) -> int:
    """Top up one user's balance. Returns that user's new balance."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    with db.tx() as c:
        c.execute("INSERT INTO flt_ledger(created_at, kind, amount, plan, note, by_user, user_id) "
                  "VALUES (?,?,?,?,?,?,?)", (now(), "topup", amount, plan, (note or None), by_user, user_id))
    log.info("FLT plan added", extra={"amount": amount, "plan": plan, "user_id": user_id})
    return balance(db, user_id).balance


def charge_row(db: Database, row_id: int, job_id: int) -> bool:
    """1 FLT from the job owner's balance for a row that got its result. At most once per row;
    nothing is charged while FLT isn't active (such rows stay free). Returns True if charged now."""
    active = is_active(db)
    owner = _job_owner(db, job_id)
    with db.tx() as c:
        first = c.execute("UPDATE rows SET flt_charged_at=? WHERE id=? AND flt_charged_at IS NULL",
                          (now(), row_id)).rowcount == 1
        if first and active:
            c.execute("INSERT OR IGNORE INTO flt_ledger(created_at, kind, amount, job_id, row_id, user_id) "
                      "VALUES (?,?,?,?,?,?)", (now(), "charge", -1, job_id, row_id, owner))
    return first and active


def is_admin(settings: Settings, email: str | None) -> bool:
    """Only the FLT admin (settings.flt_admin_email) may add FLT."""
    admin = settings.flt_admin_email.strip().lower()
    return bool(admin) and (email or "").strip().lower() == admin


def ledger(db: Database, limit: int = 200, user_id=_ALL_USERS) -> list:
    """Top-ups and adjustments one by one, charges summed per job and day (newest first). Pass a
    user_id (or None for the unowned pool) to scope it to one user; the default covers everyone."""
    if user_id is _ALL_USERS:
        pred, args = "1=1", ()
    else:
        pred, args = _user_sql(user_id)
    return db.conn.execute(
        f"""SELECT created_at, kind, amount, plan, job_id, note, by_user, user_id FROM (
               SELECT created_at, kind, amount, plan, job_id, note, by_user, user_id
                   FROM flt_ledger WHERE kind<>'charge' AND {pred}
               UNION ALL
               SELECT MAX(created_at), 'charge', SUM(amount), NULL, job_id,
                      COUNT(*) || ' row(s) with results', NULL, user_id
               FROM flt_ledger WHERE kind='charge' AND {pred} GROUP BY job_id, user_id, substr(created_at, 1, 10))
           ORDER BY created_at DESC LIMIT ?""", (*args, *args, limit)).fetchall()
