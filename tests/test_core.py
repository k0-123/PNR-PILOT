import json
import logging
import os
import time

import pytest

from app.core.config import (
    CONFIG_DIR, ResultFieldsConfig, Settings, load_result_fields,
)
from app.core.db import MIGRATIONS, Database
from app.core.logging_setup import JsonFormatter, RedactSecretsFilter, mask
from app.core.storage import LocalStorage, StorageError, sanitize_filename, sniff_image_mime
from app.excel.writer import escape_cell


# ---- storage ----

@pytest.mark.parametrize("key", ["../outside.txt", "uploads/../../x", "/etc/passwd", "C:/Windows/x"])
def test_storage_blocks_path_traversal(tmp_path, key):
    s = LocalStorage(tmp_path / "data")
    with pytest.raises(StorageError):
        s.write_bytes(key, b"x")


@pytest.mark.parametrize("name,expected", [
    ("screen 1.png", "screen_1.png"), ("../../a.png", "a.png"), ("C:\\x\\y.jpg", "y.jpg"),
    ("...", "file"), ("ümlaut.webp", "umlaut.webp"),
])
def test_sanitize_filename(name, expected):
    assert sanitize_filename(name) == expected


def test_sniff_mime():
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\nxxxx") == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_mime(b"GIF89a") is None


def test_cleanup_deletes_only_old_files(tmp_path):
    s = LocalStorage(tmp_path / "data")
    s.write_bytes("uploads/job_1/old.png", b"x")
    s.write_bytes("uploads/job_1/new.png", b"x")
    old = time.time() - 8 * 86400
    os.utime(s.path("uploads/job_1/old.png"), (old, old))
    assert s.delete_older_than(7) == 1
    assert not s.exists("uploads/job_1/old.png") and s.exists("uploads/job_1/new.png")


# ---- db ----

def test_migrations_apply_once(tmp_path):
    path = tmp_path / "x.db"
    d = Database(path)
    assert d.schema_version == len(MIGRATIONS)
    d.close()
    d = Database(path)  # reopening must not re-run migrations
    assert d.schema_version == len(MIGRATIONS)
    assert d.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    d.close()


def test_v4_migration_maps_website_lookup_statuses(tmp_path):
    """A v3 database from the Playwright version: statuses move to the extension workflow."""
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for i, sql in enumerate(MIGRATIONS[:3], start=1):
        conn.executescript("BEGIN;" + sql + f"PRAGMA user_version = {i}; COMMIT;")
    ts = "2026-09-23T00:00:00+00:00"
    conn.execute("INSERT INTO jobs(id, name, status, created_at, updated_at) VALUES (1,'j','LOOKING_UP',?,?)",
                 (ts, ts))
    conn.execute("INSERT INTO images(id, job_id, filename, stored_path, mime_type, sha256, created_at) "
                 "VALUES (1,1,'a','a','image/png','x',?)", (ts,))
    old = ["SUCCESS", "IN_PROGRESS", "TIMEOUT", "WEBSITE_ERROR", "NOT_FOUND", "BLOCKED", "PARSE_ERROR"]
    for seq, status in enumerate(old):
        conn.execute("INSERT INTO rows(job_id, image_id, seq, extraction_status, lookup_status, extracted_at, "
                     "updated_at) VALUES (1,1,?,'READY',?,?,?)", (seq, status, ts, ts))
    conn.commit()
    conn.close()

    d = Database(path)
    assert d.schema_version == len(MIGRATIONS)
    got = [r[0] for r in d.conn.execute("SELECT lookup_status FROM rows ORDER BY seq")]
    assert got == ["PARSED", "NOT_STARTED", "NOT_STARTED", "NOT_STARTED", "NOT_FOUND", "BLOCKED", "PARSE_ERROR"]
    assert d.get_job(1)["status"] == "READY_FOR_LOOKUP"
    d.close()


def test_update_job_rejects_unknown_columns(db):
    job = db.create_job("t")
    with pytest.raises(ValueError):
        db.update_job(job, total_cost_usd=5)


# ---- logging ----

def _record(msg, *args, **extra):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    rec.__dict__.update(extra)
    return rec


def test_json_log_format_includes_extra():
    out = json.loads(JsonFormatter().format(_record("hello %s", "x", job_id=3)))
    assert out["msg"] == "hello x" and out["job_id"] == 3 and out["level"] == "INFO"


def test_secrets_redacted_from_logs():
    rec = _record("key=%s", "sk-secret-123")
    RedactSecretsFilter(["sk-secret-123"]).filter(rec)
    assert "sk-secret-123" not in rec.getMessage()


def test_mask():
    assert mask("SHUKLA") == "SH****" and mask("BOGZGQ") == "BO****" and mask(None) == "<empty>"


# ---- excel escaping ----

def test_escape_cell():
    for bad in ["=1+1", "+1", "-1", "@SUM(A1)"]:
        assert escape_cell(bad) == "'" + bad
    assert escape_cell("SHUKLA") == "SHUKLA"
    assert escape_cell(-5) == -5
    assert escape_cell(None) is None and escape_cell("") is None


# ---- YAML config ----

def test_shipped_configs_load():
    fields = load_result_fields(CONFIG_DIR / "result_fields.yaml")
    assert [f.key for f in fields.fields] == ["full_name", "e_ticket_number", "frequent_flyer_program",
                                              "primary_contact", "origin", "destination",
                                              "flight_details", "operating_flight", "booking_status"]
    assert not (CONFIG_DIR / "website.yaml").exists()  # no automated website lookups


def test_default_models_are_flash_without_thinking():
    s = Settings(_env_file=None)
    assert s.gemini_model_extract == "gemini-3.6-flash" and s.gemini_thinking_extract == "off"
    assert s.gemini_thinking_result == "off" and s.gemini_concurrency == 8


@pytest.mark.parametrize("fields", [
    [], [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}],
    [{"key": "Bad Key", "label": "x"}], [{"key": "confidence", "label": "x"}],
])
def test_result_fields_validation(fields):
    with pytest.raises(ValueError):
        ResultFieldsConfig.model_validate({"fields": fields})
