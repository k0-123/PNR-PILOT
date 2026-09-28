"""Remove personal details the app doesn't need from captured booking pages before they are saved.

Booking pages (e.g. Malaysia Airlines "Passenger details") show passport and birth data. None of
it is a result field, so it is never stored: the label stays, the value becomes "[removed]".
"""
from __future__ import annotations

import re

REMOVED = "[removed]"

# A label, an optional colon, then the value: the rest of that line, or the next line when the
# label ends its line ("Date of birth\n13/08/1993"). Values may be glued to the label
# ("Date of birth13/08/1993"), as page text often is.
_PERSONAL = re.compile(
    r"(?im)(passport\s*(?:number|no\.?|expiry(?:\s*date)?)|date\s*of\s*birth|nationality|issuing\s*country)"
    r"[ \t]*:?[ \t]*(?:\n[ \t]*)?[^\n]*")


def redact_personal(text: str) -> str:
    return _PERSONAL.sub(lambda m: f"{m.group(1)}: {REMOVED}", text)
