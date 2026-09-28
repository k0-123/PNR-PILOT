"""Excel output (openpyxl). Every text cell is escaped against formula injection.

Phase 1 writes Excel #1; the final Excel, Summary sheet and CSV reuse `write_workbook`
in phase 3.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

FORMULA_PREFIXES = ("=", "+", "-", "@")

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
STATUS_FILLS = {
    "READY": "E2EFDA", "APPROVED": "E2EFDA", "PARSED": "E2EFDA",
    "NEEDS_REVIEW": "FFF2CC", "NOT_FOUND": "FFF2CC", "MISMATCH": "F8CBAD", "BLOCKED": "F8CBAD",
    "DUPLICATE": "EDEDED", "REJECTED": "F8CBAD",
}

# (header, row key) — exact column list from PLAN section 12
EXTRACTED_COLUMNS: list[tuple[str, str]] = [
    ("Image name", "image_filename"),
    ("Line No", "line_no"),
    ("Pax", "pax_count"),
    ("Surname", "surname"),
    ("First Name", "first_name"),
    ("Title", "title"),
    ("PNR", "pnr"),
    ("Class", "class_code"),
    ("Status", "status"),
    ("Date", "date"),
    ("Office ID", "office_id"),
    ("Surname Confidence", "surname_confidence"),
    ("PNR Confidence", "pnr_confidence"),
    ("Extraction Status", "extraction_status"),
    ("Notes", "notes"),
]


def escape_cell(value):
    """Prefix text that a spreadsheet would treat as a formula with a quote. Nulls -> empty."""
    if value is None:
        return None
    if isinstance(value, str):
        if value == "":
            return None
        if value.startswith(FORMULA_PREFIXES):
            return "'" + value
    return value


@dataclass
class Sheet:
    title: str
    headers: Sequence[str]
    rows: Sequence[Sequence] = field(default_factory=list)
    status_column: str | None = None  # header whose value picks the row colour


def write_workbook(path: Path, sheets: list[Sheet]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for sheet in sheets:
        _write_sheet(wb.create_sheet(sheet.title[:31]), sheet)
    wb.save(path)
    return path


def _write_sheet(ws, sheet: Sheet) -> None:
    headers = list(sheet.headers)
    ws.append([escape_cell(h) for h in headers])
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    status_idx = headers.index(sheet.status_column) if sheet.status_column in headers else None
    widths = [len(str(h)) for h in headers]
    for values in sheet.rows:
        values = [None if v == "" else v for v in values]
        ws.append(values)
        for cell, v in zip(ws[ws.max_row], values):
            if isinstance(v, str) and v.startswith(FORMULA_PREFIXES):
                # Text like "+60 12..." or "=..." stays text: stored as a string with Excel's hidden
                # quote prefix, so no apostrophe shows in the cell and nothing is evaluated.
                cell.value = v
                cell.data_type = "s"
                cell.quotePrefix = True
        for i, v in enumerate(values):
            if v is not None:
                widths[i] = max(widths[i], len(str(v)))
        if status_idx is not None and values[status_idx] in STATUS_FILLS:
            fill = PatternFill("solid", fgColor=STATUS_FILLS[values[status_idx]])
            for cell in ws[ws.max_row]:
                cell.fill = fill

    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(8, w + 2), 60)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def extracted_table(db_rows) -> tuple[list[str], list[list]]:
    headers = [h for h, _ in EXTRACTED_COLUMNS]
    rows = []
    for r in db_rows:
        vals = []
        for _, key in EXTRACTED_COLUMNS:
            v = r[key]
            if key.endswith("_confidence") and v is not None:
                v = round(float(v), 2)
            vals.append(v)
        rows.append(vals)
    return headers, rows


def write_extracted_excel(db_rows, path: Path) -> Path:
    """Excel #1 (`job_<id>_extracted.xlsx`): the original AI values for every row."""
    headers, rows = extracted_table(db_rows)
    return write_workbook(path, [Sheet("Extracted", headers, rows, status_column="Extraction Status")])


# ------------------------------------------------------------------ final Excel

def final_table(db_rows, result_fields) -> tuple[list[str], list[list]]:
    """Who the row is (image, line, names, PNR), then what the lookup found (one column per
    configured result field, lookup status, error), then the rest of Excel #1 (original AI
    values), edited values, attempts/time, who looked it up and the source. The found values sit
    right after the PNR so they are on screen when the file opens."""
    base_headers, base_rows = extracted_table(db_rows)
    split = base_headers.index("PNR") + 1
    headers = [*base_headers[:split],
               *[f.label for f in result_fields.fields], "Lookup Status", "Error",
               *base_headers[split:], "Edited Surname", "Edited PNR",
               "Attempts", "Looked Up At", "Looked Up By", "Source"]
    rows = []
    for r, base in zip(db_rows, base_rows):
        try:
            data = json.loads(r["result_json"]) if r["result_json"] else {}
        except (TypeError, ValueError):
            data = {}
        eligible = r["extraction_status"] in ("READY", "APPROVED")
        rows.append([
            *base[:split],
            *[data.get(f.key) for f in result_fields.fields],
            r["lookup_status"] if eligible else None, r["error_message"],
            *base[split:], r["edited_surname"], r["edited_pnr"],
            r["attempts"] or None, r["lookup_finished_at"],
            _get(r, "looked_up_by"), _source(r) if eligible else None,
        ])
    return headers, rows


def _get(r, key):
    try:
        return r[key]
    except (IndexError, KeyError):
        return None


def _source(r) -> str | None:
    """Where a row's results came from: the website page it was read from, or the GDS screen."""
    if r["result_json"] is None:
        return None
    shot = _get(r, "screenshot_path") or ""
    if shot.startswith("screens/"):
        return "GDS screen: " + shot.rsplit("/", 1)[-1].split("_", 1)[-1]
    return _get(r, "page_url") or ("website page" if _get(r, "page_text_path") else None)


def write_final_excel(path: Path, headers, rows, summary: list[tuple[str, object]]) -> Path:
    """`job_<id>_final.xlsx`: Results sheet + Summary sheet."""
    return write_workbook(path, [
        Sheet("Results", headers, rows, status_column="Lookup Status"),
        Sheet("Summary", ["Metric", "Value"], [list(p) for p in summary]),
    ])


def table_to_csv_bytes(headers, rows) -> bytes:
    """CSV (UTF-8 with BOM so Excel opens it correctly), formula-escaped like the xlsx."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([escape_cell(h) for h in headers])
    for row in rows:
        w.writerow(["" if (v := escape_cell(c)) is None else v for c in row])
    return buf.getvalue().encode("utf-8-sig")
