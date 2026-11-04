import httpx

from app import signups
from app.core.config import Settings
from app.core.db import Database
from app.core.models import ExtractionStatus


ACTIVATION_HTML = ('<p>Dear JESSICA,</p><a href="https://www.saudia.com/en/activate?token=abc&amp;x=1" '
                   'style="background:green">Activate Account</a><a href="https://saudia.com/privacy">Privacy</a>')
WELCOME_TEXT = "Welcome to Alfursan loyalty program. Your membership number: 1007190715. Enjoy."


def test_parsers():
    assert signups.activation_link(ACTIVATION_HTML, None) == "https://www.saudia.com/en/activate?token=abc&x=1"
    assert signups.membership_number(WELCOME_TEXT) == "1007190715"
    assert signups.membership_number("Dear JESSICA, activate your account") is None
    assert signups.host_allowed("https://x.saudia.com/a") and not signups.host_allowed("https://evil.com/saudia.com")
    assert signups.slug("Jessica Ann") == "jessicaann"


def _job_with_row(db: Database) -> tuple[int, int]:
    from tests.conftest import make_job
    job = make_job(db, [("SMITH", "7PZ5FA")])
    row = db.get_rows(job)[0]["id"]
    db.review_row(row, surname="SMITH", first_name="JESSICA", pnr="7PZ5FA", status=ExtractionStatus.APPROVED)
    return job, row


def test_full_flow(tmp_path):
    db = Database(tmp_path / "app.db")
    job, row = _job_with_row(db)
    settings = Settings(data_dir=tmp_path, mailflare_api_key="k")
    info = signups.create(db, settings, job, row)
    assert info["email"] == "jessica@pnrcaptain.com" and info["last_name"] == "SMITH"
    assert signups.create(db, settings, job, row)["email"] == info["email"]  # same address again

    to = info["email"].upper()
    msgs = [{"id": "m2", "toAddr": to, "subject": "Welcome to Alfursan loyalty program", "textBody": WELCOME_TEXT},
            {"id": "m1", "toAddr": to, "subject": "Account Activation", "htmlBody": ACTIVATION_HTML}]
    opened = []

    def handler(req: httpx.Request):
        if "/api/v1/messages" in str(req.url):
            return httpx.Response(200, json={"messages": msgs})
        opened.append(str(req.url))
        return httpx.Response(200, text="Your account is active")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert signups.poll_mail(db, settings, client) == 2
    assert opened == ["https://www.saudia.com/en/activate?token=abc&x=1"]
    su = signups.get_by_row(db, row)
    assert su["status"] == "CAPTURED" and su["membership_no"] == "1007190715"
    assert db.get_row(row)["alfursan_no"] == "1007190715"
    assert signups.poll_mail(db, settings, client) == 0  # nothing open any more
