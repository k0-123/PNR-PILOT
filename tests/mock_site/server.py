"""Tiny local booking-search website for tests and local demos. NOT used in production.

What it returns depends on the PNR entered:
    NF....   -> "booking not found" page (also when surname is NOBODY)
    SLOW..   -> answers after SLOW_SECONDS (triggers TIMEOUT)
    CAPT..   -> fake CAPTCHA page (BLOCKED)
    DENY..   -> "Access denied" page, HTTP 403 (BLOCKED)
    MISM..   -> a result page for a *different* booking (MISMATCH)
    ERR5..   -> HTTP 500 (WEBSITE_ERROR)
    BLNK..   -> blank page that never shows a result (TIMEOUT)
    FLKY..   -> first request per PNR is slow, the next one works (retry succeeds)
    anything else -> a booking result, rendered by JavaScript ~300 ms after load

Run for local demos:  python -m tests.mock_site.server   (http://127.0.0.1:8765)
"""
from __future__ import annotations

import hashlib
import html
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SLOW_SECONDS = 6.0
DEFAULT_PORT = 8765

FORM = """<!doctype html><html><head><title>Manage booking</title></head><body>
<div id="cookie-banner">We use cookies. <button id="cookie-accept"
  onclick="document.getElementById('cookie-banner').remove()">Accept</button></div>
<h1>Manage your booking</h1>
<!-- Like the real airline page: the same fields repeat in a hidden tab (e.g. "Check-in"). -->
<form action="/checkin" method="get" style="display:none">
  <input id="surname" name="surname"><input id="pnr" name="pnr" maxlength="6">
</form>
<!-- Like MH's home page: it opens on another tab; the booking form is under "My booking". -->
<div role="tablist">
  <button type="button" id="tab-book" onclick="showTab('book')">Book a flight</button>
  <button type="button" id="tab-manage" onclick="showTab('manage')"><span>My booking</span></button>
</div>
<div id="panel-book"><p>Where would you like to fly?</p></div>
<div id="panel-manage" style="display:none">
<form action="/search" method="get">
  <label>Last name <input id="surname" name="surname"></label>
  <label>Booking reference <input id="pnr" name="pnr" maxlength="6"></label>
  <button id="search-btn" type="submit" disabled>Find booking</button>
</form></div>
<script>
// Like MH: the search button stays disabled until both fields are filled.
document.addEventListener('input', function () {
  document.getElementById('search-btn').disabled =
    !(document.querySelector('#panel-manage #surname').value && document.querySelector('#panel-manage #pnr').value);
});
function showTab(t) {
  document.getElementById('panel-book').style.display = t === 'book' ? '' : 'none';
  document.getElementById('panel-manage').style.display = t === 'manage' ? '' : 'none';
}
</script></body></html>"""

NOT_FOUND = """<!doctype html><html><body><h1>Manage your booking</h1>
<div id="not-found" class="alert">We could not find a booking with these details.</div>
</body></html>"""

CAPTCHA = """<!doctype html><html><body><h1>Security check</h1>
<div class="g-recaptcha">Please verify you are human. Unusual traffic from your network.</div>
</body></html>"""

DENIED = """<!doctype html><html><body><h1>Access denied</h1>
<p>Access denied (error code 15). This request was blocked by the security rules.</p>
</body></html>"""

BLANK = "<!doctype html><html><body><div id='app'></div></body></html>"

RESULT = """<!doctype html><html><body><h1>Your booking</h1><div id="loading">Loading...</div>
<script>
setTimeout(function () {{
  document.getElementById('loading').remove();
  var d = document.createElement('div');
  d.id = 'result';
  d.innerHTML = {body!r};
  document.body.appendChild(d);
}}, 300);
</script></body></html>"""


def booking_details(surname: str, pnr: str) -> str:
    """Deterministic fake booking for a PNR."""
    h = int(hashlib.sha256(pnr.encode()).hexdigest(), 16)
    routes = [("DEL", "LHR"), ("BOM", "DXB"), ("BLR", "SIN"), ("MAA", "FRA"), ("HYD", "JFK")]
    org, dst = routes[h % len(routes)]
    fl1 = f"AI {100 + h % 800}"
    fl2 = f"AI {100 + (h // 7) % 800}"
    day = 1 + h % 28
    tkt = f"098-{h % 10**10:010d}"
    first = html.escape(surname.upper())
    return (f"<p>Booking reference: <b>{html.escape(pnr)}</b></p>"
            f"<p>Status: CONFIRMED</p>"
            f"<table><tr><th>Flight</th><th>From</th><th>To</th><th>Date</th></tr>"
            f"<tr><td>{fl1}</td><td>{org}</td><td>{dst}</td><td>{day:02d} FEB 2027</td></tr>"
            f"<tr><td>{fl2}</td><td>{dst}</td><td>{org}</td><td>{day + 1:02d} MAR 2027</td></tr></table>"
            f"<p>Passengers: {first}/PASSENGER</p><p>Ticket: {tkt}</p>"
            f"<p>Passport number: P{h % 10**7:07d}</p><p>Date of birth: 13/08/1993</p>"  # must not be stored
            # folded section (closed <details>): not in innerText, like MH's passenger accordions
            f"<details><summary>Contact details</summary><p>Contact email: {first.lower()}@example.com</p></details>")


class Handler(BaseHTTPRequestHandler):
    hits: Counter = Counter()  # PNR -> number of searches (tests check idempotency)
    lock = threading.Lock()

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _send(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, FORM)
        if url.path != "/search":
            return self._send(404, "<h1>Not found</h1>")
        q = parse_qs(url.query)
        surname = (q.get("surname") or [""])[0].strip().upper()
        pnr = (q.get("pnr") or [""])[0].strip().upper()
        with Handler.lock:
            Handler.hits[pnr] += 1
            n = Handler.hits[pnr]

        if pnr.startswith("SLOW") or (pnr.startswith("FLKY") and n == 1):
            time.sleep(SLOW_SECONDS)
        if pnr.startswith("CAPT"):
            return self._send(200, CAPTCHA)
        if pnr.startswith("DENY"):
            return self._send(403, DENIED)
        if pnr.startswith("MISM"):
            return self._send(200, RESULT.format(body=booking_details("OTHER", "ZZZ999")))
        if pnr.startswith("ERR5"):
            return self._send(500, "<h1>Internal Server Error</h1>")
        if pnr.startswith("BLNK"):
            return self._send(200, BLANK)
        if pnr.startswith("NF") or surname == "NOBODY" or not pnr:
            return self._send(200, NOT_FOUND)
        return self._send(200, RESULT.format(body=booking_details(surname, pnr)))


def start_server(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start in a background thread. Returns (server, base_url)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", DEFAULT_PORT), Handler)
    print(f"Mock booking site on http://127.0.0.1:{DEFAULT_PORT}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
