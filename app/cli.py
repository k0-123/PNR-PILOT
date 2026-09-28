"""Command line interface.

    python -m app.cli extract IMAGE [IMAGE ...] [--name NAME]
    python -m app.cli extract --job 3 [IMAGE ...]     # add images / retry failed ones
    python -m app.cli rows --job 3                    # print a job's rows again
    python -m app.cli check                           # verify API key + configured models
    python -m app.cli export --job 3                  # (re)write final Excel + CSV
    python -m app.cli screens --job 3 RT1.png RT2.png # booking details from GDS PNR screenshots
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import get_settings
from app.core.db import Database
from app.core.logging_setup import setup_logging
from app.core.storage import LocalStorage, StorageError
from app.extraction.gemini_client import GeminiClient, GeminiError
from app.extraction.pipeline import add_image_paths, extract_job
from app.jobs import export_final

ROW_COLUMNS = [("line", "line_no", 4), ("pax", "pax_count", 3), ("surname", "surname", 18),
               ("first", "first_name", 12), ("ttl", "title", 4), ("pnr", "pnr", 6),
               ("cl", "class_code", 2), ("st", "status", 2), ("date", "date", 5),
               ("office", "office_id", 10), ("s.conf", "surname_confidence", 6),
               ("p.conf", "pnr_confidence", 6), ("result", "extraction_status", 12)]


def print_rows(rows) -> None:
    def cell(v, w):
        if v is None:
            v = "-"
        elif isinstance(v, float):
            v = f"{v:.2f}"
        return str(v)[:w].ljust(w)

    print("  ".join(h.ljust(w) for h, _, w in ROW_COLUMNS) + "  notes")
    for r in rows:
        print("  ".join(cell(r[k], w) for _, k, w in ROW_COLUMNS) + "  " + (r["notes"] or ""))


def check(settings) -> int:
    """Send a tiny structured request to each configured model with its thinking setting."""
    from pydantic import BaseModel

    from app.core.costs import extract_prices, result_prices

    class Ping(BaseModel):
        ok: bool

    try:
        client = GeminiClient(settings)
    except GeminiError as exc:
        print(f"FAIL: {exc}")
        return 2
    failed = 0
    for step, model, thinking, prices in (
        ("extract", settings.gemini_model_extract, settings.gemini_thinking_extract, extract_prices(settings)),
        ("result", settings.gemini_model_result, settings.gemini_thinking_result, result_prices(settings)),
    ):
        try:
            client.generate_json(['Return {"ok": true}'], Ping, model=model, prices=prices,
                                 purpose="check", thinking=thinking)
            print(f"OK    {step:8} {model} (thinking={thinking})")
        except GeminiError as exc:
            failed += 1
            print(f"FAIL  {step:8} {model} (thinking={thinking}): {exc}")
    return 1 if failed else 0


def screens(db, storage, settings, job_id: int, images: list[Path]) -> int:
    """Read GDS PNR screenshots in this process (the worker normally does this)."""
    from app.core.config import load_result_fields
    from app.jobs import JobActionError, add_pnr_screens
    from app.lookup.gds_screens import process_screen, unmatched_list

    try:
        ids = add_pnr_screens(db, storage, settings, job_id, [(p.name, p.read_bytes()) for p in images])
        client = GeminiClient(settings)
    except (JobActionError, StorageError, GeminiError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    fields = load_result_fields()
    for sid in ids:
        screen = db.conn.execute("SELECT * FROM pnr_screens WHERE id=?", (sid,)).fetchone()
        if screen["status"] == "DONE":
            continue
        db.conn.execute("UPDATE pnr_screens SET status='PROCESSING' WHERE id=?", (sid,))
        db.conn.commit()
        print(f"Reading {screen['filename']}...", file=sys.stderr)
        process_screen(db, storage, settings, client, fields, screen)
    for s in db.get_pnr_screens(job_id):
        if s["id"] in ids:
            print(f"{s['filename']}: {s['status']} | PNR(s) {s['pnrs'] or '-'} | {s['matched_rows']} row(s) matched"
                  + (f" | unmatched: {', '.join(unmatched_list(s))}" if unmatched_list(s) else "")
                  + (f" | {s['error']}" if s["error"] else ""))
    xlsx, csv = export_final(db, storage, job_id, fields)
    print(f"Job {job_id} -> {db.get_job(job_id)['status']}\nFinal Excel: {storage.path(xlsx)}\nCSV: {storage.path(csv)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract", help="read GDS screenshots with Gemini and write Excel #1")
    ex.add_argument("images", nargs="*", type=Path)
    ex.add_argument("--job", type=int, help="existing job id (add images / retry failed ones)")
    ex.add_argument("--name", help="name for a new job")

    rw = sub.add_parser("rows", help="print the rows of a job")
    rw.add_argument("--job", type=int, required=True)

    sub.add_parser("check", help="verify the API key and that the configured models answer")

    xp = sub.add_parser("export", help="write the final Excel + CSV for a job")
    xp.add_argument("--job", type=int, required=True)

    sc = sub.add_parser("screens", help="read GDS PNR display screenshots into a job's results")
    sc.add_argument("--job", type=int, required=True)
    sc.add_argument("images", nargs="+", type=Path)

    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    db = Database(settings.db_path)
    storage = LocalStorage(settings.data_dir)

    if args.cmd == "check":
        return check(settings)

    if args.cmd in ("export", "screens") and not db.get_job(args.job):
        parser.error(f"job {args.job} not found")
    if args.cmd == "screens":
        return screens(db, storage, settings, args.job, args.images)
    if args.cmd == "export":
        xlsx, csv = export_final(db, storage, args.job)
        print(f"Final Excel: {storage.path(xlsx)}\nCSV: {storage.path(csv)}")
        return 0

    if args.cmd == "rows":
        if not db.get_job(args.job):
            parser.error(f"job {args.job} not found")
        print_rows(db.get_rows(args.job))
        return 0

    if args.job is None and not args.images:
        parser.error("give one or more image files, or --job ID to resume a job")
    missing = [str(p) for p in args.images if not p.is_file()]
    if missing:
        parser.error("file(s) not found: " + ", ".join(missing))

    if args.job is not None:
        if not db.get_job(args.job):
            parser.error(f"job {args.job} not found")
        job_id = args.job
    else:
        job_id = db.create_job(args.name or args.images[0].stem)

    try:
        add_image_paths(db, storage, settings, job_id, args.images)
    except StorageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        client = GeminiClient(settings)
    except GeminiError as exc:
        client = None  # cached images can still be processed
        print(f"Warning: {exc}", file=sys.stderr)

    def progress(done: int, total: int, msg: str) -> None:
        print(f"[{done}/{total}] {msg}", file=sys.stderr, flush=True)

    s = extract_job(db, storage, settings, client, job_id, progress=progress)

    print()
    print_rows(db.get_rows(job_id))
    for img in db.get_images(job_id):
        if img["error"]:
            print(f"\nImage {img['filename']} FAILED: {img['error']}")
    print(
        f"\nJob {s.job_id} -> {s.status.value}\n"
        f"Images: {s.images_extracted}/{s.images_total} extracted, {s.images_failed} failed\n"
        f"Rows:   {s.rows_total} total, {s.rows_ready} ready, {s.rows_review} need review, "
        f"{s.rows_duplicate} duplicate\n"
        f"Gemini cost: ${s.cost_usd:.4f}\n"
        f"Excel #1: {storage.path(s.excel_key) if s.excel_key else '(no rows)'}"
    )
    return 1 if s.images_failed else 0


if __name__ == "__main__":
    sys.exit(main())
