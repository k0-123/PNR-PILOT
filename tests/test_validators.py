import pytest

from app.core.models import ExtractionStatus, GeminiRow
from app.extraction.validators import split_title, validate_row
from tests.conftest import gemini_row

READY, REVIEW = ExtractionStatus.READY, ExtractionStatus.NEEDS_REVIEW


def v(**kw):
    return validate_row(GeminiRow(**gemini_row(**kw)), threshold=0.85)


def test_clean_row_is_ready():
    r = v()
    assert r.extraction_status == READY and r.issues == [] and r.warnings == []
    assert (r.surname, r.pnr, r.pax_count) == ("SHUKLA", "BOGZGQ", 2)


def test_normalises_case_and_spaces():
    r = v(surname="  kishtappa   naidu ", pnr=" 9p4bie ", first_name="h")
    assert (r.surname, r.pnr, r.first_name) == ("KISHTAPPA NAIDU", "9P4BIE", "H")
    assert r.extraction_status == READY


@pytest.mark.parametrize("pnr", ["BOGZGQ", "8SQLQK", "9P4BIE", "123456"])
def test_good_pnrs(pnr):
    assert v(pnr=pnr).extraction_status == READY


@pytest.mark.parametrize("pnr", ["BOGZG", "BOGZGQ1", "BOG-GQ", "BOGZ Q", "BÖGZGQ", None, ""])
def test_bad_pnrs_need_review(pnr):
    r = v(pnr=pnr)
    assert r.extraction_status == REVIEW
    assert any("PNR" in i for i in r.issues)


@pytest.mark.parametrize("surname", ["SHUKLA", "KISHTAPPA NAIDU", "SMITH-JONES", "O'NEIL"])
def test_good_surnames(surname):
    assert v(surname=surname).extraction_status == READY


@pytest.mark.parametrize("surname", ["SHUK1A", "-SMITH", "02SHUKLA", "SMITH/J", None])
def test_bad_surnames_need_review(surname):
    r = v(surname=surname)
    assert r.extraction_status == REVIEW
    assert any("surname" in i for i in r.issues)


@pytest.mark.parametrize("pax,ok", [(1, True), (99, True), (None, True), (0, False), (100, False)])
def test_pax_range(pax, ok):
    assert (v(pax_count=pax).extraction_status == READY) is ok


@pytest.mark.parametrize("field", ["surname_confidence", "pnr_confidence"])
def test_confidence_threshold(field):
    assert v(**{field: 0.84}).extraction_status == REVIEW
    assert v(**{field: None}).extraction_status == REVIEW
    assert v(**{field: 0.85}).extraction_status == READY


def test_confidence_clamped():
    r = v(surname_confidence=7, pnr_confidence=-1)
    assert (r.surname_confidence, r.pnr_confidence) == (1.0, 0.0)


@pytest.mark.parametrize("first,expected", [
    ("MALINI MS", ("MALINI", "MS")),
    ("RAJESH MR", ("RAJESH", "MR")),
    ("ANITA MRS", ("ANITA", "MRS")),
    ("ARJUN MSTR", ("ARJUN", "MSTR")),
    ("PRIYA MISS", ("PRIYA", "MISS")),
    ("RAVI M", ("RAVI", "M")),
    ("M", ("M", None)),              # single-letter first name, not a title
    ("WILLIAMS", ("WILLIAMS", None)),  # ends in MS but is a name
    ("MALINIMS", ("MALINIMS", None)),  # glued title is not guessed
    (None, (None, None)),
])
def test_split_title(first, expected):
    assert split_title(first) == expected


def test_title_from_row_and_from_gemini():
    assert (v(first_name="MALINI MS").first_name, v(first_name="MALINI MS").title) == ("MALINI", "MS")
    r = v(first_name="MALINI", title="ms")
    assert (r.first_name, r.title) == ("MALINI", "MS")


def test_cut_off_name_is_not_a_title():
    # job 4: the GDS list cuts names at the column edge; Gemini called the leftover a title
    r = v(first_name="DIPIKA", title="MA")
    assert (r.first_name, r.title, r.extraction_status) == ("DIPIKA MA", None, READY)
    assert "name may be cut off on screen" in r.warnings
    assert (v(first_name="MANGESH", title="R").first_name, v(first_name="MANGESH", title="R").title) == ("MANGESH R", None)


def test_format_warnings_do_not_block():
    r = v(office_id="X", date="14FEBR", line_no="86", class_code="UU", status="H")
    assert r.extraction_status == READY
    assert len(r.warnings) == 5


def test_notes_combine_issues_and_ai_notes():
    r = v(pnr=None, notes="PNR hidden by cursor")
    assert "PNR missing" in r.notes_text and "PNR hidden by cursor" in r.notes_text
