"""SQLite (WAL) persistence with simple versioned migrations.

The `jobs` table doubles as the extraction queue (claimed by the worker). Original AI
values in `rows` are never updated; reviewer edits go into the edited_* columns.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import LOOKUP_RETRYABLE, ExtractedRow, ExtractionStatus, ImageStatus, JobStatus, LookupStatus

# Append new migrations; never edit one that has shipped. PRAGMA user_version tracks progress.
MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE jobs (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        name                      TEXT NOT NULL,
        status                    TEXT NOT NULL DEFAULT 'PENDING',
        website_config_name       TEXT NOT NULL DEFAULT 'website',
        error_message             TEXT,
        total_cost_usd            REAL NOT NULL DEFAULT 0,
        created_at                TEXT NOT NULL,
        updated_at                TEXT NOT NULL,
        extraction_started_at     TEXT,
        extraction_finished_at    TEXT,
        lookup_started_at         TEXT,
        lookup_finished_at        TEXT
    );

    CREATE TABLE images (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id                 INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        filename               TEXT NOT NULL,
        stored_path            TEXT NOT NULL,
        mime_type              TEXT NOT NULL,
        sha256                 TEXT NOT NULL,
        status                 TEXT NOT NULL DEFAULT 'PENDING',
        raw_ai_response        TEXT,
        from_cache             INTEGER NOT NULL DEFAULT 0,
        tokens_in              INTEGER NOT NULL DEFAULT 0,
        tokens_out             INTEGER NOT NULL DEFAULT 0,
        cost_usd               REAL NOT NULL DEFAULT 0,
        attempts               INTEGER NOT NULL DEFAULT 0,
        error                  TEXT,
        created_at             TEXT NOT NULL,
        extraction_started_at  TEXT,
        extracted_at           TEXT,
        UNIQUE (job_id, sha256)
    );
    CREATE INDEX idx_images_sha ON images(sha256);

    CREATE TABLE rows (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id              INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        image_id            INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        seq                 INTEGER NOT NULL,
        line_no             TEXT,
        pax_count           INTEGER,
        surname             TEXT,
        first_name          TEXT,
        title               TEXT,
        pnr                 TEXT,
        class_code          TEXT,
        status              TEXT,
        date                TEXT,
        office_id           TEXT,
        surname_confidence  REAL,
        pnr_confidence      REAL,
        notes               TEXT,
        issues              TEXT NOT NULL DEFAULT '[]',
        edited_surname      TEXT,
        edited_first_name   TEXT,
        edited_pnr          TEXT,
        extraction_status   TEXT NOT NULL,
        duplicate_of        INTEGER REFERENCES rows(id) ON DELETE SET NULL,
        lookup_status       TEXT NOT NULL DEFAULT 'NOT_STARTED',
        error_message       TEXT,
        attempts            INTEGER NOT NULL DEFAULT 0,
        extracted_at        TEXT NOT NULL,
        reviewed_at         TEXT,
        lookup_started_at   TEXT,
        lookup_finished_at  TEXT,
        updated_at          TEXT NOT NULL
    );
    CREATE INDEX idx_rows_job ON rows(job_id, extraction_status, lookup_status);

    CREATE TABLE lookup_results (
        row_id           INTEGER PRIMARY KEY REFERENCES rows(id) ON DELETE CASCADE,
        result_json      TEXT,
        page_text_path   TEXT,
        screenshot_path  TEXT,
        tokens_in        INTEGER NOT NULL DEFAULT 0,
        tokens_out       INTEGER NOT NULL DEFAULT 0,
        cost_usd         REAL NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL
    );

    CREATE TABLE ai_calls (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id       INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
        image_id     INTEGER REFERENCES images(id) ON DELETE SET NULL,
        row_id       INTEGER REFERENCES rows(id) ON DELETE SET NULL,
        purpose      TEXT NOT NULL,
        model        TEXT NOT NULL,
        tokens_in    INTEGER NOT NULL,
        tokens_out   INTEGER NOT NULL,
        cost_usd     REAL NOT NULL,
        duration_ms  INTEGER NOT NULL,
        success      INTEGER NOT NULL,
        error        TEXT,
        created_at   TEXT NOT NULL
    );
    CREATE INDEX idx_ai_calls_job ON ai_calls(job_id);
    """,
    # v2: job queue claiming, worker heartbeats
    """
    ALTER TABLE jobs ADD COLUMN lookup_requested INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN worker_id TEXT;
    ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT;
    CREATE INDEX idx_jobs_status ON jobs(status);
    CREATE TABLE workers (
        id          TEXT PRIMARY KEY,
        pid         INTEGER,
        host        TEXT,
        started_at  TEXT NOT NULL,
        last_seen   TEXT NOT NULL
    );
    """,
    # v3: GDS PNR display screenshots (booking details read from the GDS instead of a website)
    """
    CREATE TABLE pnr_screens (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        filename         TEXT NOT NULL,
        stored_path      TEXT NOT NULL,
        mime_type        TEXT NOT NULL,
        sha256           TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / PROCESSING / DONE / FAILED
        pnrs             TEXT,                              -- record locators found on the screen
        matched_rows     INTEGER NOT NULL DEFAULT 0,
        unmatched        TEXT,                              -- JSON list of passengers not matched
        raw_ai_response  TEXT,
        tokens_in        INTEGER NOT NULL DEFAULT 0,
        tokens_out       INTEGER NOT NULL DEFAULT 0,
        cost_usd         REAL NOT NULL DEFAULT 0,
        error            TEXT,
        created_at       TEXT NOT NULL,
        processed_at     TEXT,
        UNIQUE (job_id, sha256)
    );
    CREATE INDEX idx_pnr_screens_status ON pnr_screens(status);
    """,
    # v4: realplan v2 lookup statuses. Lookups are now triggered by a person in the browser
    # extension (no automated website lookups), so old in-flight/failed website lookups go back
    # to NOT_STARTED and SUCCESS becomes PARSED.
    """
    UPDATE rows SET lookup_status='PARSED' WHERE lookup_status='SUCCESS';
    UPDATE rows SET lookup_status='NOT_STARTED'
        WHERE lookup_status IN ('IN_PROGRESS','TIMEOUT','NETWORK_ERROR','WEBSITE_ERROR','FAILED');
    UPDATE jobs SET status='READY_FOR_LOOKUP', worker_id=NULL WHERE status='LOOKING_UP';
    """,
    # v5: browser-extension lookups. One `lookups` row per unique PNR of a job (created when a
    # staff member's extension claims it); row.lookup_status mirrors it for progress/Excel.
    """
    CREATE TABLE api_tokens (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        token_hash    TEXT NOT NULL UNIQUE,     -- sha256 of the token; the token itself is never stored
        created_at    TEXT NOT NULL,
        last_used_at  TEXT,
        revoked_at    TEXT
    );
    CREATE TABLE lookups (
        job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        pnr              TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'NOT_STARTED',
        token_id         INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
        looked_up_by     TEXT,                  -- token name, for the audit columns
        lease_until      TEXT,
        claimed_at       TEXT,
        page_text_path   TEXT,
        screenshot_path  TEXT,
        page_url         TEXT,
        note             TEXT,
        captures         INTEGER NOT NULL DEFAULT 0,
        captured_at      TEXT,
        finished_at      TEXT,
        updated_at       TEXT NOT NULL,
        PRIMARY KEY (job_id, pnr)
    );
    CREATE INDEX idx_lookups_status ON lookups(job_id, status);
    CREATE TABLE site_profiles (
        name          TEXT PRIMARY KEY,
        profile_json  TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    """,
    # v6: the worker parses captured pages (CAPTURED -> PARSED); a claim column stops two
    # workers from parsing the same capture.
    """
    ALTER TABLE lookups ADD COLUMN parse_worker TEXT;
    ALTER TABLE lookups ADD COLUMN parse_started_at TEXT;
    ALTER TABLE lookups ADD COLUMN parsed_at TEXT;
    """,
    # v7: FLT credits (app/credits.py). 1 FLT = 1 row that got its result. The ledger holds
    # top-ups (+) and charges (-); a row is charged at most once (unique index). No foreign keys:
    # the ledger outlives jobs.
    """
    CREATE TABLE flt_ledger (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT NOT NULL,
        kind        TEXT NOT NULL CHECK (kind IN ('topup', 'charge', 'adjust')),
        amount      INTEGER NOT NULL,
        plan        TEXT,
        job_id      INTEGER,
        row_id      INTEGER,
        note        TEXT,
        by_user     TEXT
    );
    CREATE UNIQUE INDEX idx_flt_charge_row ON flt_ledger(row_id) WHERE kind = 'charge';
    CREATE INDEX idx_flt_ledger_kind ON flt_ledger(kind, created_at);
    ALTER TABLE rows ADD COLUMN flt_charged_at TEXT;
    """,
    # v8: accounts (app/users.py). The admin creates every account (no public sign-up) and each
    # user has ONE active session (session_id) so a new device signs the old one out. Jobs, FLT
    # ledger rows and extension tokens carry the owning user_id for per-user FLT balances.
    """
    CREATE TABLE users (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash  TEXT NOT NULL,           -- pbkdf2_sha256$iters$salt$hash; the password is never stored
        is_admin       INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT NOT NULL,
        disabled_at    TEXT,
        session_id     TEXT,                    -- current single-device session; NULL = signed out
        session_at     TEXT,
        last_seen_at   TEXT
    );
    ALTER TABLE jobs ADD COLUMN user_id INTEGER REFERENCES users(id);
    ALTER TABLE flt_ledger ADD COLUMN user_id INTEGER;
    ALTER TABLE api_tokens ADD COLUMN user_id INTEGER REFERENCES users(id);
    CREATE INDEX idx_jobs_user ON jobs(user_id);
    CREATE INDEX idx_flt_ledger_user ON flt_ledger(user_id, kind);
    """,
]

_JOB_UPDATABLE = {"name", "status", "website_config_name", "error_message", "extraction_started_at",
                  "extraction_finished_at", "lookup_started_at", "lookup_finished_at",
                  "lookup_requested", "worker_id", "heartbeat_at"}

ELIGIBLE_SQL = "('READY','APPROVED')"  # extraction statuses that may be looked up
RETRYABLE_SQL = "(" + ",".join(f"'{s}'" for s in sorted(LOOKUP_RETRYABLE)) + ")"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def dedupe_key(surname: str | None, pnr: str | None,
               first_name: str | None = None) -> tuple[str, str, str] | None:
    """Duplicate key is surname + first name + PNR (never PNR alone): family members on one
    booking share surname and PNR but are different passengers. Spaces ignored: OCR may drop them."""
    if not surname or not pnr:
        return None
    return (surname.replace(" ", ""), pnr, (first_name or "").replace(" ", ""))


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # UI reads while the worker writes
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {i};\nCOMMIT;")

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    @contextmanager
    def write_tx(self):
        """Transaction that takes the write lock up front (BEGIN IMMEDIATE), for read-then-write
        sequences that must not interleave with another process (e.g. claiming PNRs)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ jobs
    def create_job(self, name: str, website_config_name: str = "website",
                   user_id: int | None = None) -> int:
        ts = now()
        with self.tx() as c:
            return c.execute(
                "INSERT INTO jobs(name, website_config_name, created_at, updated_at, user_id) "
                "VALUES (?,?,?,?,?)",
                (name, website_config_name, ts, ts, user_id),
            ).lastrowid

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def list_jobs(self, user_id: int | None = None) -> list[sqlite3.Row]:
        if user_id is None:
            return self.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
        return self.conn.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC",
                                 (user_id,)).fetchall()

    def update_job(self, job_id: int, **fields) -> None:
        bad = set(fields) - _JOB_UPDATABLE
        if bad:
            raise ValueError(f"cannot update job fields: {sorted(bad)}")
        fields = {k: (v.value if isinstance(v, JobStatus) else v) for k, v in fields.items()}
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {cols}, updated_at=? WHERE id=?",
                      (*fields.values(), now(), job_id))

    # ---------------------------------------------------------------- images
    def add_image(self, job_id: int, filename: str, stored_path: str, mime_type: str,
                  sha256: str) -> tuple[int, bool]:
        """Register an image. Returns (id, created); the same file twice in a job is ignored."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM images WHERE job_id=? AND sha256=?",
                            (job_id, sha256)).fetchone()
            if row:
                return row["id"], False
            cur = c.execute(
                """INSERT INTO images(job_id, filename, stored_path, mime_type, sha256, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (job_id, filename, stored_path, mime_type, sha256, now()),
            )
            return cur.lastrowid, True

    def get_images(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM images WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    def count_images(self, job_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM images WHERE job_id=?", (job_id,)).fetchone()[0]

    def find_cached_extraction(self, sha256: str) -> str | None:
        """Raw AI response from any earlier successful extraction of the same image."""
        row = self.conn.execute(
            """SELECT raw_ai_response FROM images
               WHERE sha256=? AND status=? AND raw_ai_response IS NOT NULL
               ORDER BY id LIMIT 1""",
            (sha256, ImageStatus.EXTRACTED.value),
        ).fetchone()
        return row["raw_ai_response"] if row else None

    def find_cached_screen_extraction(self, sha256: str) -> str | None:
        """Raw AI response from any earlier successful read of the same GDS PNR screen.

        Lets an identical screenshot (re-uploaded, or re-run after a config change) be reused
        without calling Gemini again. The screen being processed is PROCESSING, not DONE, so it
        never matches itself."""
        row = self.conn.execute(
            """SELECT raw_ai_response FROM pnr_screens
               WHERE sha256=? AND status='DONE' AND raw_ai_response IS NOT NULL
               ORDER BY id LIMIT 1""",
            (sha256,),
        ).fetchone()
        return row["raw_ai_response"] if row else None

    def mark_image_extracting(self, image_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE images SET status=?, extraction_started_at=?, error=NULL WHERE id=?",
                      (ImageStatus.EXTRACTING.value, now(), image_id))

    def mark_image_failed(self, image_id: int, error: str, attempts: int,
                          raw_ai_response: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE images SET status=?, error=?, attempts=attempts+?,
                       raw_ai_response=COALESCE(?, raw_ai_response) WHERE id=?""",
                (ImageStatus.EXTRACTION_FAILED.value, error, attempts, raw_ai_response, image_id),
            )

    def save_image_extraction(self, job_id: int, image_id: int, rows: list[ExtractedRow], *,
                              raw_ai_response: str, tokens_in: int, tokens_out: int,
                              cost_usd: float, attempts: int, from_cache: bool) -> None:
        """Store an image's rows and mark it EXTRACTED in one transaction."""
        ts = now()
        with self.tx() as c:
            c.execute("DELETE FROM rows WHERE image_id=?", (image_id,))
            for seq, r in enumerate(rows):
                c.execute(
                    """INSERT INTO rows(job_id, image_id, seq, line_no, pax_count, surname, first_name,
                           title, pnr, class_code, status, date, office_id, surname_confidence,
                           pnr_confidence, notes, issues, extraction_status, extracted_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, image_id, seq, r.line_no, r.pax_count, r.surname, r.first_name, r.title,
                     r.pnr, r.class_code, r.status, r.date, r.office_id, r.surname_confidence,
                     r.pnr_confidence, r.notes_text or None, json.dumps(r.issues),
                     r.extraction_status.value, ts, ts),
                )
            c.execute(
                """UPDATE images SET status=?, raw_ai_response=?, from_cache=?, tokens_in=?,
                       tokens_out=?, cost_usd=?, attempts=attempts+?, error=NULL, extracted_at=?
                   WHERE id=?""",
                (ImageStatus.EXTRACTED.value, raw_ai_response, int(from_cache), tokens_in,
                 tokens_out, cost_usd, attempts, ts, image_id),
            )
        self.refresh_duplicates(job_id)

    # ------------------------------------------------------------------ rows
    _ROW_SELECT = """SELECT r.*, i.filename AS image_filename, i.stored_path AS image_path,
                      COALESCE(r.edited_surname, r.surname) AS effective_surname,
                      COALESCE(r.edited_first_name, r.first_name) AS effective_first_name,
                      COALESCE(r.edited_pnr, r.pnr) AS effective_pnr,
                      lr.result_json, lr.page_text_path, lr.screenshot_path,
                      lk.looked_up_by, lk.page_url
               FROM rows r JOIN images i ON i.id = r.image_id
               LEFT JOIN lookup_results lr ON lr.row_id = r.id
               LEFT JOIN lookups lk ON lk.job_id = r.job_id AND lk.pnr = COALESCE(r.edited_pnr, r.pnr)"""

    def get_rows(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"{self._ROW_SELECT} WHERE r.job_id=? ORDER BY r.image_id, r.seq", (job_id,)
        ).fetchall()

    def get_row(self, row_id: int) -> sqlite3.Row | None:
        return self.conn.execute(f"{self._ROW_SELECT} WHERE r.id=?", (row_id,)).fetchone()

    def review_row(self, row_id: int, *, surname: str | None, first_name: str | None,
                   pnr: str | None, status: ExtractionStatus) -> None:
        """Store reviewer edits next to (never over) the original AI values.

        Callers must re-validate the edited values first (phase 3 review screen).
        """
        with self.tx() as c:
            c.execute(
                """UPDATE rows SET edited_surname=?, edited_first_name=?, edited_pnr=?,
                       extraction_status=?, duplicate_of=NULL, reviewed_at=?, updated_at=?
                   WHERE id=?""",
                (surname, first_name, pnr, status.value, now(), now(), row_id),
            )
            job_id = c.execute("SELECT job_id FROM rows WHERE id=?", (row_id,)).fetchone()[0]
        self.refresh_duplicates(job_id)

    def refresh_duplicates(self, job_id: int) -> None:
        """Later rows with the same effective surname + first name + PNR become DUPLICATE of the first.

        A row that stops being a duplicate (after a review edit) goes back to NEEDS_REVIEW.
        """
        rows = self.conn.execute(
            """SELECT id, extraction_status, duplicate_of,
                      COALESCE(edited_surname, surname) AS s, COALESCE(edited_pnr, pnr) AS p,
                      COALESCE(edited_first_name, first_name) AS f
               FROM rows WHERE job_id=? ORDER BY image_id, seq""",
            (job_id,),
        ).fetchall()
        first_seen: dict[tuple[str, str, str], int] = {}
        ts = now()
        with self.tx() as c:
            for r in rows:
                if r["extraction_status"] == ExtractionStatus.REJECTED:
                    continue
                key = dedupe_key(r["s"], r["p"], r["f"])
                original = first_seen.get(key) if key else None
                if original is not None:
                    if (r["extraction_status"] != ExtractionStatus.DUPLICATE
                            or r["duplicate_of"] != original):
                        c.execute(
                            "UPDATE rows SET extraction_status=?, duplicate_of=?, updated_at=? WHERE id=?",
                            (ExtractionStatus.DUPLICATE.value, original, ts, r["id"]),
                        )
                    continue
                if key:
                    first_seen[key] = r["id"]
                if r["extraction_status"] == ExtractionStatus.DUPLICATE:
                    c.execute(
                        "UPDATE rows SET extraction_status=?, duplicate_of=NULL, updated_at=? WHERE id=?",
                        (ExtractionStatus.NEEDS_REVIEW.value, ts, r["id"]),
                    )

    def row_counts(self, job_id: int) -> dict[str, int]:
        counts = {s.value: 0 for s in ExtractionStatus} | {s.value: 0 for s in LookupStatus}
        for col in ("extraction_status", "lookup_status"):
            for r in self.conn.execute(
                f"SELECT {col} AS s, COUNT(*) AS n FROM rows WHERE job_id=? GROUP BY {col}", (job_id,)
            ):
                counts[r["s"]] = r["n"]
        counts["total"] = self.conn.execute(
            "SELECT COUNT(*) FROM rows WHERE job_id=?", (job_id,)).fetchone()[0]
        return counts

    # --------------------------------------------------------- job queue
    def claim_job(self, worker_id: str) -> sqlite3.Row | None:
        """Atomically take the oldest job waiting for extraction (PENDING -> EXTRACTING).
        Two workers never get the same job. Lookups are not worker jobs: staff run them
        from the browser extension."""
        ts = now()
        with self.tx() as c:
            row = c.execute(
                """UPDATE jobs SET status='EXTRACTING', worker_id=?, heartbeat_at=?, updated_at=?
                   WHERE id = (SELECT id FROM jobs WHERE status='PENDING' ORDER BY id LIMIT 1)
                   RETURNING id""",
                (worker_id, ts, ts),
            ).fetchone()
        return self.get_job(row["id"]) if row else None

    def transition_job(self, job_id: int, from_statuses, to_status, **fields) -> bool:
        """Change status only if the job is currently in one of `from_statuses`."""
        bad = set(fields) - _JOB_UPDATABLE
        if bad:
            raise ValueError(f"cannot update job fields: {sorted(bad)}")
        froms = [str(s) for s in from_statuses]
        sets = ", ".join(["status=?", *(f"{k}=?" for k in fields), "updated_at=?"])
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE jobs SET {sets} WHERE id=? AND status IN ({','.join('?' * len(froms))})",
                (str(to_status), *fields.values(), now(), job_id, *froms),
            )
            return cur.rowcount == 1

    def touch_job(self, job_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now(), job_id))

    def recover_stale_jobs(self, stale_seconds: float) -> list[int]:
        """Extractions whose worker stopped heartbeating (crash, restart) go back to the queue."""
        cutoff = ago(stale_seconds)
        with self.tx() as c:
            stale = [r["id"] for r in c.execute(
                """SELECT id FROM jobs WHERE status='EXTRACTING'
                   AND (heartbeat_at IS NULL OR heartbeat_at < ?)""", (cutoff,))]
            for job_id in stale:
                c.execute("UPDATE jobs SET status='PENDING', worker_id=NULL, updated_at=? WHERE id=?",
                          (now(), job_id))
        return stale

    def worker_heartbeat(self, worker_id: str, pid: int, host: str) -> None:
        ts = now()
        with self.tx() as c:
            c.execute("""INSERT INTO workers(id, pid, host, started_at, last_seen) VALUES (?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                      (worker_id, pid, host, ts, ts))

    def workers_online(self, within_seconds: float = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM workers WHERE last_seen >= ?",
                                 (ago(within_seconds),)).fetchall()

    def list_jobs_with_counts(self, user_id: int | None = None) -> list[sqlite3.Row]:
        where = "" if user_id is None else "WHERE j.user_id=? "
        args: tuple = () if user_id is None else (user_id,)
        return self.conn.execute(
            f"""SELECT j.*,
                   (SELECT COUNT(*) FROM images i WHERE i.job_id=j.id) AS images,
                   (SELECT COUNT(*) FROM rows r WHERE r.job_id=j.id) AS rows_total,
                   (SELECT COUNT(*) FROM rows r WHERE r.job_id=j.id
                        AND r.extraction_status='NEEDS_REVIEW') AS rows_review,
                   (SELECT COUNT(*) FROM rows r WHERE r.job_id=j.id
                        AND r.lookup_status='PARSED') AS rows_success,
                   (SELECT COUNT(*) FROM rows r WHERE r.job_id=j.id
                        AND r.lookup_status='NOT_FOUND') AS rows_not_found,
                   (SELECT COUNT(*) FROM rows r WHERE r.job_id=j.id AND r.extraction_status IN {ELIGIBLE_SQL}
                        AND r.lookup_status IN {RETRYABLE_SQL}) AS rows_failed
               FROM jobs j {where}ORDER BY j.id DESC""", args
        ).fetchall()

    # -------------------------------------------------------- PNR screens
    def add_pnr_screen(self, job_id: int, filename: str, stored_path: str, mime_type: str,
                       sha256: str) -> int:
        """Register a GDS PNR screenshot (the same file twice in a job is re-queued, not duplicated)."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM pnr_screens WHERE job_id=? AND sha256=?", (job_id, sha256)).fetchone()
            if row:
                c.execute("UPDATE pnr_screens SET status='PENDING', error=NULL WHERE id=? AND status='FAILED'",
                          (row["id"],))
                return row["id"]
            return c.execute(
                """INSERT INTO pnr_screens(job_id, filename, stored_path, mime_type, sha256, created_at)
                   VALUES (?,?,?,?,?,?)""", (job_id, filename, stored_path, mime_type, sha256, now()),
            ).lastrowid

    def get_pnr_screens(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM pnr_screens WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    def claim_pnr_screen(self) -> sqlite3.Row | None:
        """Atomically take the oldest PENDING screen (PENDING -> PROCESSING)."""
        with self.tx() as c:
            row = c.execute(
                """UPDATE pnr_screens SET status='PROCESSING'
                   WHERE id = (SELECT id FROM pnr_screens WHERE status='PENDING' ORDER BY id LIMIT 1)
                   RETURNING id""").fetchone()
        return self.conn.execute("SELECT * FROM pnr_screens WHERE id=?", (row["id"],)).fetchone() if row else None

    def finish_pnr_screen(self, screen_id: int, *, status: str, pnrs: str | None = None,
                          matched_rows: int = 0, unmatched: list | None = None, raw: str | None = None,
                          tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
                          error: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE pnr_screens SET status=?, pnrs=?, matched_rows=?, unmatched=?, raw_ai_response=?,
                       tokens_in=?, tokens_out=?, cost_usd=?, error=?, processed_at=? WHERE id=?""",
                (status, pnrs, matched_rows, json.dumps(unmatched or []), raw, tokens_in, tokens_out,
                 cost_usd, error, now(), screen_id),
            )

    def recover_stale_screens(self) -> int:
        """Screens left PROCESSING by a worker that stopped go back to PENDING (called at worker start)."""
        with self.tx() as c:
            return c.execute("UPDATE pnr_screens SET status='PENDING' WHERE status='PROCESSING'").rowcount

    # ------------------------------------------------------------ lookups
    def finish_row(self, row_id: int, status, *, error: str | None = None, attempts: int = 0) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE rows SET lookup_status=?, error_message=?, attempts=attempts+?,
                       lookup_finished_at=?, updated_at=? WHERE id=?""",
                (str(status), error, attempts, now(), now(), row_id),
            )

    def save_lookup_result(self, row_id: int, *, result: dict | None, page_text_path: str | None,
                           screenshot_path: str | None, tokens_in: int = 0, tokens_out: int = 0,
                           cost_usd: float = 0.0) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO lookup_results(row_id, result_json, page_text_path, screenshot_path,
                       tokens_in, tokens_out, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(row_id) DO UPDATE SET result_json=excluded.result_json,
                       page_text_path=excluded.page_text_path, screenshot_path=excluded.screenshot_path,
                       tokens_in=lookup_results.tokens_in+excluded.tokens_in,
                       tokens_out=lookup_results.tokens_out+excluded.tokens_out,
                       cost_usd=lookup_results.cost_usd+excluded.cost_usd, created_at=excluded.created_at""",
                (row_id, json.dumps(result) if result is not None else None, page_text_path,
                 screenshot_path, tokens_in, tokens_out, cost_usd, now()),
            )

    def requeue_rows(self, job_id: int, statuses) -> int:
        """Send eligible rows (and their PNR lookups) in the given statuses back to NOT_STARTED."""
        sts = [str(s) for s in statuses]
        if not sts:
            return 0
        with self.tx() as c:
            c.execute(
                f"""UPDATE lookups SET status='NOT_STARTED', token_id=NULL, lease_until=NULL, note=NULL,
                        updated_at=? WHERE job_id=? AND status IN ({','.join('?' * len(sts))})""",
                (now(), job_id, *sts))
            cur = c.execute(
                f"""UPDATE rows SET lookup_status='NOT_STARTED', error_message=NULL, updated_at=?
                    WHERE job_id=? AND extraction_status IN {ELIGIBLE_SQL}
                    AND lookup_status IN ({','.join('?' * len(sts))})""",
                (now(), job_id, *sts),
            )
            return cur.rowcount

    def requeue_parse(self, job_id: int, statuses) -> int:
        """Send PNR lookups in `statuses` whose page text is saved back to CAPTURED, so the worker
        reads the saved page again (no new website search). Returns the number of PNRs."""
        sts = [str(s) for s in statuses]
        if not sts:
            return 0
        with self.tx() as c:
            pnrs = [r["pnr"] for r in c.execute(
                f"""SELECT pnr FROM lookups WHERE job_id=? AND page_text_path IS NOT NULL
                    AND status IN ({','.join('?' * len(sts))})""", (job_id, *sts))]
            for pnr in pnrs:
                c.execute("""UPDATE lookups SET status='CAPTURED', note=NULL, parse_worker=NULL,
                                 parse_started_at=NULL, updated_at=? WHERE job_id=? AND pnr=?""",
                          (now(), job_id, pnr))
                c.execute(f"""UPDATE rows SET lookup_status='CAPTURED', error_message=NULL, updated_at=?
                              WHERE job_id=? AND COALESCE(edited_pnr, pnr)=? AND extraction_status IN {ELIGIBLE_SQL}""",
                          (now(), job_id, pnr))
        return len(pnrs)

    def lookup_counts(self, job_id: int) -> dict[str, int]:
        """Lookup statuses of the rows that are eligible for lookup (READY/APPROVED)."""
        counts = {s.value: 0 for s in LookupStatus}
        for r in self.conn.execute(
            f"""SELECT lookup_status AS s, COUNT(*) AS n FROM rows WHERE job_id=?
                AND extraction_status IN {ELIGIBLE_SQL} GROUP BY lookup_status""", (job_id,)
        ):
            counts[r["s"]] = r["n"]
        counts["eligible"] = sum(counts[s.value] for s in LookupStatus)
        return counts

    # --------------------------------------------------------------- AI calls
    def record_ai_call(self, *, job_id: int | None, purpose: str, model: str, tokens_in: int,
                       tokens_out: int, cost_usd: float, duration_ms: int, success: bool,
                       error: str | None = None, image_id: int | None = None,
                       row_id: int | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO ai_calls(job_id, image_id, row_id, purpose, model, tokens_in,
                       tokens_out, cost_usd, duration_ms, success, error, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, image_id, row_id, purpose, model, tokens_in, tokens_out, cost_usd,
                 duration_ms, int(success), error, now()),
            )
            if job_id is not None:
                c.execute("UPDATE jobs SET total_cost_usd = total_cost_usd + ? WHERE id=?",
                          (cost_usd, job_id))

    def job_cost(self, job_id: int) -> dict:
        return dict(self.conn.execute(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tokens_in,
                      COALESCE(SUM(tokens_out),0) AS tokens_out, COALESCE(SUM(cost_usd),0) AS cost_usd
               FROM ai_calls WHERE job_id=?""",
            (job_id,),
        ).fetchone())
