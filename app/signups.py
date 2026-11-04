"""AlFursan signups: one per passenger row, linked to the row by a generated email address.

Flow: staff press "AlFursan signup" in the extension -> `create` gives the passenger's details and
an address like jessica-142-5@pnrcaptain.com -> the extension fills saudia.com/Register, staff
check it and press Confirm -> Saudia emails "Account Activation" to that address (Mailflare
catch-all) -> the worker (`poll_mail`) opens the Activate link -> AlFursan emails "Welcome" with
the membership number -> saved on the signup -> final Excel column "AlFursan No.".

Only links on Saudia/AlFursan hosts are opened by the server. Anything else, or a page Imperva
blocks, becomes NEEDS_BROWSER and the extension opens the link in the staff member's browser.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.core.db import Database, now

log = logging.getLogger("app.signups")

REGISTER_URL = "https://www.saudia.com/en-IN/Register"
OPEN_STATUSES = ("FILLED", "SUBMITTED", "ACTIVATION_SENT", "ACTIVATED", "NEEDS_BROWSER")
LINK_HOSTS = ("saudia.com", "alfursan.com", "alfursan.sa")
BLOCK_WORDS = ("pardon our interruption", "incapsula", "access denied", "request unsuccessful")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")

_MEMBER_RE = re.compile(r"(?:membership|member|alfursan|loyalty)[^0-9]{0,80}?(\d{9,12})\b", re.I | re.S)
_HREF_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def slug(name: str | None) -> str:
    s = re.sub(r"[^a-z]", "", (name or "").lower())
    return s[:20] or "pax"


def unique_email(db: Database, first: str | None, domain: str) -> str:
    """firstname@domain; jessica2@, jessica3@... when the name is taken."""
    base = slug(first)
    like = f"{base}%@{domain}"
    taken = {r[0].lower() for r in db.conn.execute(
        "SELECT email FROM signups WHERE email LIKE ? UNION SELECT email FROM batch_people WHERE email LIKE ?",
        (like, like))}
    i = 1
    while True:
        email = f"{base}{i if i > 1 else ''}@{domain}"
        if email.lower() not in taken:
            return email
        i += 1


def _row_position(db: Database, job_id: int, row_id: int) -> tuple[int, dict]:
    for i, r in enumerate(db.get_rows(job_id), start=1):
        if r["id"] == row_id:
            return i, r
    raise KeyError(row_id)


def get_by_row(db: Database, row_id: int):
    return db.conn.execute("SELECT * FROM signups WHERE row_id=?", (row_id,)).fetchone()


def create(db: Database, settings: Settings, job_id: int, row_id: int) -> dict:
    """Signup details for one row (re-uses the row's existing signup and address)."""
    n, r = _row_position(db, job_id, row_id)
    first = r["effective_first_name"] or ""
    surname = r["effective_surname"] or ""
    su = get_by_row(db, row_id)
    if su is None:
        email = unique_email(db, first or surname, settings.signup_email_domain)
        with db.write_tx() as c:
            c.execute("""INSERT INTO signups(job_id, row_id, email, created_at, updated_at)
                         VALUES (?,?,?,?,?)""", (job_id, row_id, email, now(), now()))
        su = get_by_row(db, row_id)
    return {"signup_id": su["id"], "email": su["email"], "status": su["status"],
            "title": (r["title"] or "").upper(), "first_name": first, "last_name": surname,
            "register_url": REGISTER_URL}


def rows_for_job(db: Database, job_id: int) -> list[dict]:
    sus = {s["row_id"]: s for s in db.conn.execute("SELECT * FROM signups WHERE job_id=?", (job_id,))}
    out = []
    for i, r in enumerate(db.get_rows(job_id), start=1):
        s = sus.get(r["id"])
        out.append({"row_id": r["id"], "n": i, "title": r["title"], "first_name": r["effective_first_name"],
                    "surname": r["effective_surname"], "signup_status": s["status"] if s else None,
                    "email": s["email"] if s else None, "membership_no": s["membership_no"] if s else None})
    return out


def needs_browser(db: Database, user_id: int | None) -> list[dict]:
    """Activation links the server could not open. id is 'j<n>' (job signup) or 'b<n>' (batch)."""
    rows = db.conn.execute(
        """SELECT 'j' || s.id AS id, s.email, s.activation_url FROM signups s JOIN jobs j ON j.id = s.job_id
           WHERE s.status='NEEDS_BROWSER' AND s.activation_url IS NOT NULL AND j.user_id IS ?
           UNION ALL
           SELECT 'b' || p.id, p.email, p.activation_url FROM batch_people p
           JOIN signup_batches b ON b.id = p.batch_id
           WHERE p.status='NEEDS_BROWSER' AND p.activation_url IS NOT NULL AND b.user_id IS ?""",
        (user_id, user_id)).fetchall()
    return [dict(r) for r in rows]


def mark_opened(db: Database, ref: str, user_id: int | None) -> bool:
    kind, num = ref[:1], ref[1:]
    if kind not in ("j", "b") or not num.isdigit():
        return False
    table, owner = (("signups", "job_id IN (SELECT id FROM jobs WHERE user_id IS ?)") if kind == "j" else
                    ("batch_people", "batch_id IN (SELECT id FROM signup_batches WHERE user_id IS ?)"))
    with db.write_tx() as c:
        cur = c.execute(f"""UPDATE {table} SET status='ACTIVATED', note='opened in browser', updated_at=?
                            WHERE id=? AND status='NEEDS_BROWSER' AND {owner}""", (now(), int(num), user_id))
    return cur.rowcount > 0


# ------------------------------------------------------------------ email parsing
def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</td>", "\n", h)
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", h))


def membership_number(text: str) -> str | None:
    m = _MEMBER_RE.search(text)
    return m.group(1) if m else None


def activation_link(html: str | None, text: str | None) -> str | None:
    """The 'Activate Account' button's link (anchor text or URL mentions activat/verif/confirm)."""
    for href, label in _HREF_RE.findall(html or ""):
        label_t = html_to_text(label).lower()
        href = html_lib.unescape(href)
        if href.startswith("http") and re.search(r"activat|verif|confirm", label_t + " " + href.lower()):
            return href
    for url in _URL_RE.findall(text or ""):
        if re.search(r"activat|verif|confirm", url, re.I):
            return url
    return None


def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in LINK_HOSTS)


def open_link(url: str, client: httpx.Client) -> tuple[bool, str]:
    """GET the activation link like a browser would. Returns (worked, note)."""
    if not host_allowed(url):
        return False, f"link host {urlparse(url).hostname} is not Saudia/AlFursan: open it in the browser"
    try:
        res = client.get(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}, follow_redirects=True)
    except httpx.HTTPError as exc:
        return False, f"could not open link: {exc.__class__.__name__}"
    if not host_allowed(str(res.url)):
        return False, "link redirected off Saudia: open it in the browser"
    body = res.text[:20000].lower()
    if res.status_code >= 400 or any(w in body for w in BLOCK_WORDS):
        return False, f"Saudia answered {res.status_code} / blocked: open it in the browser"
    return True, f"activation link opened ({res.status_code})"


def handle_message(db: Database, su, msg: dict, client: httpx.Client, table: str = "signups") -> None:
    html = msg.get("htmlBody") or ""
    text = msg.get("textBody") or html_to_text(html)
    full = f"{msg.get('subject') or ''}\n{text}\n{html_to_text(html) if html else ''}"
    fields: dict = {}
    number = membership_number(full)
    if number:
        fields = {"status": "CAPTURED", "membership_no": number, "note": "membership number captured"}
    elif link := activation_link(html, text):
        ok, note = open_link(link, client)
        fields = {"status": "ACTIVATED" if ok else "NEEDS_BROWSER", "activation_url": link, "note": note}
    seen = json.loads(su["seen_messages"] or "[]") + [msg.get("id")]
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.write_tx() as c:
        c.execute(f"UPDATE {table} SET {sets + ', ' if sets else ''}seen_messages=?, updated_at=? WHERE id=?",
                  (*fields.values(), json.dumps(seen[-50:]), now(), su["id"]))
    log.info("Signup email handled", extra={"signup_id": su["id"], "status": fields.get("status", "-")})


def poll_mail(db: Database, settings: Settings, client: httpx.Client | None = None) -> int:
    """Read recent inbound mail from Mailflare and act on mail for open signups. Returns handled count."""
    key = settings.mailflare_api_key.get_secret_value()
    if not key:
        return 0
    marks = ",".join("?" * len(OPEN_STATUSES))
    by_email: dict = {}
    for table in ("signups", "batch_people"):
        for row in db.conn.execute(f"SELECT * FROM {table} WHERE status IN ({marks})", OPEN_STATUSES):
            by_email[row["email"].lower()] = (table, row)
    if not by_email:
        return 0
    own = client or httpx.Client(timeout=20)
    handled = 0
    try:
        res = own.get(f"{settings.mailflare_url.rstrip('/')}/api/v1/messages",
                      params={"direction": "inbound", "limit": 100},
                      headers={"Authorization": f"Bearer {key}"})
        res.raise_for_status()
        msgs = res.json().get("messages", [])
        for msg in reversed(msgs):  # oldest first: activation before welcome
            to = set(re.findall(r"[\w.+-]+@[\w.-]+", (msg.get("toAddr") or "").lower()))
            for email, (table, su) in list(by_email.items()):
                if email in to and msg.get("id") not in json.loads(su["seen_messages"] or "[]"):
                    handle_message(db, su, msg, own, table)
                    fresh = db.conn.execute(f"SELECT * FROM {table} WHERE id=?", (su["id"],)).fetchone()
                    by_email[email] = (table, fresh)
                    handled += 1
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Mailflare poll failed", extra={"error": str(exc)[:200]})
    finally:
        if client is None:
            own.close()
    return handled
