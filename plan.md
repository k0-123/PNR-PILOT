Stop. Don't sync the spec to the current code. Do the opposite: realplan.md (v2) is the target, and the code must move toward it.

Keep and reuse what already works:
- Gemini photo extraction, validation, review, and Excel generation from the existing code (move/wrap it as needed).
- Streamlit can stay as the dashboard for now if that's faster. But add a small FastAPI service for the extension API described in section 10 of realplan.md.

Change:
- Remove/disable the automated Playwright lookup against Malaysia Airlines. Playwright stays ONLY for tests against the local mock site.
- Build the Chrome extension exactly as in section 7 of realplan.md, including the human-in-the-loop rule (section 0 rule 4 and 7.4): the extension never submits the search; staff presses Enter.
- Switch the default Gemini models to gemini-2.5-flash (configurable via .env), thinking off.

First, show me a short plan listing: which existing files you keep, what you change, what you remove, and the order of work. Then wait for my approval before editing anything.