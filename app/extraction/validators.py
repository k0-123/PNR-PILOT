"""Normalisation and validation of extracted rows (PLAN section 7)."""
from __future__ import annotations

import re

from app.core.models import ExtractedRow, ExtractionStatus, GeminiRow

PNR_RE = re.compile(r"^[A-Z0-9]{6}$")
SURNAME_RE = re.compile(r"^[A-Z][A-Z \-']*$")
LINE_NO_RE = re.compile(r"^\d{3}$")
CLASS_RE = re.compile(r"^[A-Z]$")
STATUS_RE = re.compile(r"^[A-Z]{2}$")
DATE_RE = re.compile(r"^\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)$")
OFFICE_RE = re.compile(r"^[A-Z0-9]{4,10}$")
TITLES = ("MSTR", "MISS", "MRS", "MR", "MS", "M")


def normalise(value) -> str | None:
    """Uppercase, trim, collapse whitespace; empty -> None."""
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip().upper()
    return value or None


def split_title(first_name: str | None) -> tuple[str | None, str | None]:
    """Split a trailing title word off the first name: 'MALINI MS' -> ('MALINI', 'MS').

    Only a separate word is treated as a title, and only when a name remains before it, so a
    single-letter first name like 'M' is kept. Titles glued to the name ('MALINIMS') are left
    alone: names are often truncated and real names end in 'MS'/'M' (e.g. 'WILLIAMS', 'PREM').
    """
    if not first_name:
        return first_name, None
    parts = first_name.split(" ")
    if len(parts) >= 2 and parts[-1] in TITLES:
        return " ".join(parts[:-1]), parts[-1]
    return first_name, None


def validate_row(raw: GeminiRow, threshold: float) -> ExtractedRow:
    issues: list[str] = []    # force NEEDS_REVIEW
    warnings: list[str] = []  # shown in notes only

    surname = normalise(raw.surname)
    pnr = normalise(raw.pnr)
    first_name, title = split_title(normalise(raw.first_name))
    ai_title = normalise(raw.title)
    if ai_title and not title:
        if ai_title in TITLES:
            title = ai_title
        else:
            # Not a title: the name was cut off at the screen's column edge ("CHAUHAN/DIPIKA MA",
            # "CHAUHAN/MANGESH R"). Keep it as part of the first name, as displayed.
            first_name = f"{first_name} {ai_title}" if first_name else ai_title
            warnings.append("name may be cut off on screen")

    if surname is None:
        issues.append("surname missing")
    elif not SURNAME_RE.match(surname):
        issues.append("surname has invalid characters")

    if pnr is None:
        issues.append("PNR missing")
    elif not PNR_RE.match(pnr):
        issues.append("PNR must be exactly 6 letters/digits")

    pax = raw.pax_count
    if pax is not None and not 1 <= pax <= 99:
        issues.append("pax count must be 1-99")

    s_conf, p_conf = _conf(raw.surname_confidence), _conf(raw.pnr_confidence)
    if s_conf is None or s_conf < threshold:
        issues.append(f"low surname confidence ({_fmt(s_conf)})")
    if p_conf is None or p_conf < threshold:
        issues.append(f"low PNR confidence ({_fmt(p_conf)})")

    line_no, class_code, status = normalise(raw.line_no), normalise(raw.class_code), normalise(raw.status)
    date, office_id = normalise(raw.date), normalise(raw.office_id)
    for label, value, rx in (("line no", line_no, LINE_NO_RE), ("class", class_code, CLASS_RE),
                             ("status", status, STATUS_RE), ("date", date, DATE_RE),
                             ("office ID", office_id, OFFICE_RE)):
        if value is not None and not rx.match(value):
            warnings.append(f"unexpected {label} format")

    return ExtractedRow(
        line_no=line_no, pax_count=pax, surname=surname, first_name=first_name, title=title,
        pnr=pnr, class_code=class_code, status=status, date=date, office_id=office_id,
        surname_confidence=s_conf, pnr_confidence=p_conf, ai_notes=normalise_notes(raw.notes),
        issues=issues, warnings=warnings,
        extraction_status=ExtractionStatus.NEEDS_REVIEW if issues else ExtractionStatus.READY,
    )


def validate_edit(surname: str | None, pnr: str | None) -> tuple[str | None, str | None, list[str]]:
    """Re-validate reviewer input. Returns (normalised surname, normalised PNR, errors)."""
    surname, pnr = normalise(surname), normalise(pnr)
    errors = []
    if not surname or not SURNAME_RE.match(surname):
        errors.append("Surname must start with a letter and contain only letters, spaces, - or '")
    if not pnr or not PNR_RE.match(pnr):
        errors.append("PNR must be exactly 6 letters/digits (A-Z, 0-9)")
    return surname, pnr, errors


def normalise_notes(notes: str | None) -> str | None:
    if not notes:
        return None
    notes = re.sub(r"\s+", " ", notes).strip()
    return notes or None


def _conf(v) -> float | None:
    if v is None:
        return None
    try:
        return min(max(float(v), 0.0), 1.0)
    except (TypeError, ValueError):
        return None


def _fmt(v: float | None) -> str:
    return "none" if v is None else f"{v:.2f}"
