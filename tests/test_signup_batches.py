"""Batch signups. Test data only (never sent anywhere): the IDs below are made up for the tests."""
from io import BytesIO

import httpx
from openpyxl import Workbook, load_workbook

from app import signup_batches as sb
from app import signups
from app.core.config import Settings
from app.core.db import Database
from app.core.models import ExtractionStatus, LookupStatus
from tests.conftest import make_job


def _xlsx(rows) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_map_headers():
    cols = sb.map_headers(["S.No", "Title", "First Name", "Surname", "Nationality", "Passport No", "Mobile"])
    assert cols == {"title": 1, "first_name": 2, "last_name": 3, "nationality": 4, "id_number": 5, "phone": 6}


def test_upload_next_and_capture(tmp_path):
    db = Database(tmp_path / "app.db")
    settings = Settings(data_dir=tmp_path, mailflare_api_key="k")
    data = _xlsx([["Passenger list"], ["Title", "First Name", "Last Name", "Passport"],
                  ["MS", "Jessica", "Smith", "K547212"], [None, None, None, None],
                  ["MR", "Jessica", "Brown", "T000001"]])
    info = sb.create_batch(db, settings, None, "test", data)
    assert info["people"] == 2 and info["columns"]["id_number"] == "Passport"

    p1 = sb.next_person(db, info["batch_id"], None)
    assert (p1["email"], p1["first_name"], p1["id_number"]) == ("jessica@pnrcaptain.com", "Jessica", "K547212")
    assert sb.set_person_status(db, p1["person_id"], None, "SUBMITTED")
    p2 = sb.next_person(db, info["batch_id"], None)
    assert p2["email"] == "jessica2@pnrcaptain.com"  # same first name -> numbered
    assert sb.next_person(db, info["batch_id"], None) is None

    msgs = [{"id": "w", "toAddr": "Jessica <JESSICA@pnrcaptain.com>", "subject": "Welcome to Alfursan",
             "textBody": "Your Alfursan membership number is 1007190715"}]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"messages": msgs})))
    assert signups.poll_mail(db, settings, client) == 1

    out = sb.batch_excel(db, info["batch_id"], None, tmp_path / "out.xlsx")
    rows = list(load_workbook(out).active.iter_rows(values_only=True))
    assert rows[0][-4:] == ("Email", "AlFursan No.", "Signup Status", "Note")
    assert rows[1][-4:-1] == ("jessica@pnrcaptain.com", "1007190715", "CAPTURED")
    assert rows[2][-3] is None


def test_batch_from_green_rows(tmp_path):
    db = Database(tmp_path / "app.db")
    settings = Settings(data_dir=tmp_path)
    job = make_job(db, [("SMITH", "7PZ5FA"), ("BROWN", "7PZ5FB")])
    r1, r2 = (r["id"] for r in db.get_rows(job))
    db.review_row(r1, surname="SMITH", first_name="ANNA", pnr="7PZ5FA", status=ExtractionStatus.APPROVED)
    db.conn.execute("UPDATE rows SET lookup_status=? WHERE id=?", (LookupStatus.PARSED.value, r1))
    db.conn.commit()
    info = sb.create_batch_from_job(db, settings, None, job)
    assert info["people"] == 1  # only the green row
    p = sb.next_person(db, info["batch_id"], None)
    assert (p["email"], p["last_name"]) == ("anna@pnrcaptain.com", "SMITH")
    db.conn.execute("UPDATE batch_people SET membership_no='1007190715' WHERE id=?", (p["person_id"],))
    db.conn.commit()
    assert db.get_row(r1)["alfursan_no"] == "1007190715" and db.get_row(r2)["alfursan_no"] is None


def test_two_phones_and_stuck_person_comes_back(tmp_path):
    """Two phones never get the same person; a person left FILLED (app closed) returns after 15 min."""
    db = Database(tmp_path / "app.db")
    settings = Settings(data_dir=tmp_path)
    data = _xlsx([["First Name", "Last Name"], ["Ali", "Khan"], ["Sara", "Khan"]])
    bid = sb.create_batch(db, settings, None, "two phones", data)["batch_id"]

    phone1 = sb.next_person(db, bid, None)
    phone2 = sb.next_person(db, bid, None)
    assert phone1["person_id"] != phone2["person_id"]
    assert sb.next_person(db, bid, None) is None  # both in progress, nothing left

    # phone 2 crashed 20 minutes ago without Stop -> its person is handed out again
    with db.write_tx() as c:
        c.execute("UPDATE batch_people SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                  (phone2["person_id"],))
    again = sb.next_person(db, bid, None)
    assert again["person_id"] == phone2["person_id"]
    assert sb.next_person(db, bid, None) is None  # fresh FILLED rows are not handed out
