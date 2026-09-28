# GDS Screenshot → PNR Lookup Automation

```
GDS photo(s) → Gemini reads every passenger row → validation + human review → Excel #1
  → Playwright opens the website, fills SURNAME + PNR → result page (text + screenshot)
  → Gemini extracts the configured fields → Final Excel (+ CSV)
```

The spec is in [realplan.md](realplan.md). Phases 1–4 are built. For Malaysia Airlines, booking details come from **GDS PNR screenshots** (section 3), because their website blocks automated lookups. The website-lookup engine stays available for sites that allow it, and can be tested against a local **mock** booking site.

> ⚠️ Use a **paid** Gemini API tier for real passenger data. The free tier may use content to improve Google products.
> ⚠️ Some airline sites block data-centre IPs. **Test a few lookups from the VPS before any bulk run.**

---

## 1. Local setup (Windows)

```powershell
cd C:\Users\Karan\OneDrive\Desktop\sheet
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env          # then set GEMINI_API_KEY, APP_EMAIL, APP_PASSWORD
python -m app.cli check         # verifies the API key and both models
```

On Linux or macOS, activate with `source .venv/bin/activate` and use `cp` instead of `copy`.

### Run it

Double-click **`start_local.bat`**, or open two terminals:

```powershell
python -m app.worker            # terminal 1: does all the heavy work
streamlit run app/ui.py         # terminal 2: the web UI → http://localhost:8501
```

To test lookups without the real website, also double-click **`start_mock_site.bat`** (or run `python -m tests.mock_site.server`). Then pick **"Mock booking site (mock_demo)"** as the website when you create a job.

### Tests

```powershell
python -m pytest -q             # ~2 min; real Chromium against the mock site, Gemini faked
```

---

## 2. Using the app

1. **Sign in** with `APP_EMAIL` / `APP_PASSWORD` from `.env`. After 5 wrong tries, sign-in locks for 60 s.
2. **New job:** upload the GDS photos (PNG/JPG/JPEG/WEBP), choose the website and click **Create job**. The worker reads each image, which takes about 20–30 s with the Pro model. **You can close the tab; the job keeps running.**
3. **Review:** rows the AI was unsure about are shown next to their photo. Fix the surname or PNR, then **Approve** or **Reject**. **Approve all valid** approves every flagged row whose values already pass validation. A PNR that doesn't match `^[A-Z0-9]{6}$` can't be approved.
4. **Open for lookups** on the Job details page. If some rows still need review, the button says "skip N unreviewed"; rows needing review are **never** sent to the website.
5. **Browser extension (side panel):** pick the job and the website, then click **Start**. For each booking the extension opens the booking form (e.g. MH's "My booking" tab), fills Last name + Booking reference, and waits:
   - **Auto-continue off (default):** you press **Enter**. The extension reads the result page, goes back and fills the next booking.
   - **Auto-continue on** (switch in the side panel): the extension presses **Continue** itself, at least `auto_submit.min_gap_ms` apart (6 s for Malaysia Airlines). It switches itself **off at the first CAPTCHA / block**. Searching faster raises the risk of being blocked by the airline.
   - Hotkeys on the airline page: **Alt+N** not found, **Alt+S** skip, **Alt+B** back, **Alt+P** pause, **Alt+R** re-capture.
6. **Job details:** live progress bars and a row table that refreshes every 3 s, plus the job's cost. Buttons: **Pause / Resume / Cancel / Retry failed rows**.
7. **Results:** search and filter the rows, then download **Final Excel**, **Final CSV** or **Excel #1**. The website values (Full Name … Booking Status) come right after the PNR. **🔁 Fix & retry** lists rows that were not found, mismatched or skipped: correct the surname/PNR against the photo and the row is searched again.

| Row status | Meaning |
|---|---|
| READY / APPROVED | Will be looked up |
| NEEDS_REVIEW | Low confidence or invalid surname/PNR. Needs a human |
| DUPLICATE | Same surname, first name **and** PNR as an earlier row. Family members on one booking (same surname and PNR, different first names) are *not* duplicates |
| REJECTED | Excluded by the reviewer |
| PARSED / NOT_FOUND | Lookup finished (NOT_FOUND can be corrected with **Fix & retry**) |
| MISMATCH / SKIPPED / BLOCKED / PARSE_ERROR | Finished without a result; **Retry failed rows** queues only these |

**If the website shows a CAPTCHA** or an "unusual traffic" page, the booking becomes BLOCKED, the job **pauses** and Auto-continue switches off. Solve it in the browser, then press **Resume** in the side panel.

Other built-in safeguards:
- **No double lookups:** a booking that was captured is never searched again (except Back / Re-capture).
- **Re-parse:** a PARSE_ERROR is read again from its saved page text on retry, without visiting the website again.
- **Privacy:** passport numbers, dates of birth, nationality and passport expiry are removed from captured pages before they are saved.

---

## 3. Booking details from GDS PNR screens (recommended for Malaysia Airlines)

Malaysia Airlines' manage-booking page is protected by Imperva. It answers automated browsers with **"Access denied (error code 15)"**. That's a hard block with no CAPTCHA, so the website lookup can't be used there, and the app deliberately doesn't try to get around it. The same details are already in your GDS PNR instead:

1. Create the job as usual (upload the passenger-list photo) and review flagged rows.
2. In the GDS, display each booking in full (Amadeus `RT <PNR>`) and take a screenshot.
3. On **Job details**, open **"Add booking details from GDS PNR screens"**, upload the screenshots and click **Read screens**. One screenshot can hold one or several bookings.
4. The worker reads each screen with the extraction model (Gemini Pro). It matches every passenger to a row by **PNR + surname**, using the first name when family members share a surname. It then fills the columns from `config/result_fields.yaml`: Full Name, E-Ticket Number, Frequent Flyer Program, Primary Contact Details, From, To, Flight Details and Booking Status.
5. When every row has its details, the job becomes COMPLETED. Download the final Excel from **Results**.

Where the details come from on the screen:
- Names come from the name elements (`1.CHEN/LIJING MS`).
- E-ticket numbers come from the `FA PAX 232-…` lines.
- Frequent flyer numbers come from the `SSR FQTV` lines.
- Contacts come from the `AP`/`APE`/`APM` lines.
- Flights and route come from the segment lines.

How results are checked and reported:
- Values read with low confidence are marked "check values" in the Error column.
- Passengers that match no row are listed next to the screenshot.
- Rows still needing review are never filled.

Command line equivalent: `python -m app.cli screens --job 2 RT_ER7P5B.png`

## 4. Configuring a website (for sites that allow automated lookups)

Everything site-specific lives in **`config/website.yaml`**. Nothing is hard-coded. To fill it in:

1. Open the site in Chrome, right-click the Surname field → **Inspect**, and copy a stable selector (an `#id` is best). Do the same for the PNR field, the submit button, the result area and the "not found" message.
2. Replace every `TODO` value. Lookups refuse to run while any TODO remains.
3. `before_fill` holds optional clicks, e.g. accepting a cookie banner (`optional: true` means "skip if it isn't there").
4. Start with 5 real rows (Phase 5 in the spec), check the Final Excel, then run in bulk.

Key settings: `timeout_ms`, `retries` (for timeouts and network errors), `delay_between_searches_ms` (be polite), `concurrency` (keep 1 unless the site allows more), `headless`, and `dedupe_lookups_by_pnr` (look each PNR up once and copy the result to other rows with that PNR).

- **A second website** is just another file, `config/websites/<name>.yaml`, with the same keys. It then appears in the website list when you create a job.
- `config/websites/mock_demo.yaml` is for local demos only. **Delete it in production.**

**Result fields:** edit **`config/result_fields.yaml`** (`key`, `label`, optional `description`). The Gemini prompt and schema, and the Final Excel columns, are generated from this file.

## 5. Choosing / changing the Gemini model

Models, thinking level and prices are all in `.env`:

```
GEMINI_MODEL_EXTRACT=gemini-3.1-pro-preview   GEMINI_THINKING_EXTRACT=low   PRICE_INPUT_PER_M / PRICE_OUTPUT_PER_M
GEMINI_MODEL_RESULT=gemini-3.1-flash-lite     GEMINI_THINKING_RESULT=off    PRICE_RESULT_INPUT_PER_M / ..._OUTPUT_PER_M
```

After a change, run `python -m app.cli check` and restart the worker. Keep in mind:
- Pro models need thinking `low` or higher.
- Google retires models, so `check` catches a 404 early. For example, `gemini-2.5-flash` is already gone.
- Update the `PRICE_*` values so cost numbers stay correct.

The defaults come from a benchmark on 2026-09-23 using synthetic phone photos with glare, a cursor and many 0/O, 1/I, 8/B, 5/S characters:

| Model (thinking) | PNRs correct | Wrong but not flagged | ≈ cost / image |
|---|---|---|---|
| **gemini-3.1-pro-preview (low)** | **44/45** | **1** | $0.044 |
| gemini-3.8-flash (low) | 41/45 | 3 | $0.014 (price doubles 1 Jan 2027) |
| gemini-3.5-flash (off) | 41/45 | 4 | $0.03 |

The extraction prompt is `app/extraction/prompts/extract_rows.txt`, and the result-page prompt is `extract_result.txt` in the same folder.

---

## 6. VPS deployment (Ubuntu + Docker + HTTPS)

**1. Install Docker**

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER    # log out and back in
```

**2. Copy the project and set up `.env`**

```bash
scp -r sheet/ user@your-vps:~/gds          # or git clone; don't copy .venv/ or data/
cd ~/gds
cp .env.example .env && nano .env          # GEMINI_API_KEY, APP_EMAIL, a STRONG APP_PASSWORD
rm config/websites/mock_demo.yaml          # demo config: not for production
mkdir -p data && sudo chown -R 10001:10001 data   # the container runs as UID 10001
```

**3. Start the app**

```bash
docker compose up -d --build
docker compose ps                          # ui + worker should be "running"
docker compose exec worker python -m app.cli check
```

The UI listens only on `127.0.0.1:8501`. Put **Caddy** in front for automatic HTTPS:

```bash
sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # edit the domain name first
sudo systemctl reload caddy                      # → https://your-domain
```

Open ports 80 and 443 in the firewall (`sudo ufw allow 80,443/tcp`). Keep port 8501 closed. If you use Nginx instead of Caddy, proxy to `127.0.0.1:8501` with WebSocket upgrade headers (`proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`).

**4. Day-to-day operations**

| Task | Command |
|---|---|
| Logs | `docker compose logs -f worker` / `docker compose logs -f ui` |
| Update the app | `git pull` (or copy the files) then `docker compose up -d --build` |
| Change website/result config | edit `config/*.yaml` (mounted read-only), then `docker compose restart worker` |
| Backup | `sqlite3 data/app.db ".backup data/backup-$(date +%F).db"` (safe while running), plus copy `data/outputs/` |
| Restart | `docker compose restart` |

- **Headless only:** in Docker, `headless` must stay `true` because there is no screen. Solving a CAPTCHA by hand only works locally on Windows.
- **Restarts are safe:** if the worker restarts mid-job, the job goes back into the queue and resumes. SUCCESS rows are never repeated.

---

## 7. Security & data

- Passenger names and PNRs are personal data:
  - Logs (JSON) show only job, row and image IDs at INFO level.
  - The API key and password are redacted from every log line and never shown in the UI.
- `.env` is git-ignored and Docker-ignored.
- **File cleanup:** uploads, screenshots, page texts and output files older than `DELETE_FILES_AFTER_DAYS` (default 7) are deleted by the worker every hour.
- **File handling:** all file access goes through `app/core/storage.py`, which blocks path traversal. Uploads are checked by their content (magic bytes), size limit and count limit, and file names are cleaned.
- **Formula injection:** Excel and CSV cells starting with `= + - @` are escaped.

## 8. Command line (optional)

```powershell
python -m app.cli extract photo1.jpg photo2.png --name "Feb batch" [--site mock_demo]
python -m app.cli rows --job 1
python -m app.cli lookup --job 1 [--site tests/mock_site/config.yaml]   # runs lookups in this terminal
python -m app.cli export --job 1                                        # final Excel + CSV
python -m app.cli screens --job 1 RT1.png RT2.png                         # details from GDS PNR screens
python -m app.cli check
```

## 9. Project layout

```
app/
  ui.py              Streamlit UI (reads/writes SQLite + storage only)
  worker.py          background worker: job queue, heartbeats, crash recovery, cleanup
  jobs.py            job actions: create, review, start/pause/resume/cancel, retry, export
  cli.py
  core/              config (.env + YAML), db (SQLite WAL + migrations), models/statuses,
                     storage, costs, logging (JSON), retry
  extraction/        gemini_client, image_extractor, validators, pipeline, prompts/
  lookup/            browser, website_adapter (config-driven), result_extractor, engine
  excel/             writer: Excel #1, Final Excel + Summary sheet, CSV
config/              website.yaml (placeholders), result_fields.yaml, websites/*.yaml
tests/               pytest suite + mock_site/ (local booking site used by tests and demos)
deploy/Caddyfile     HTTPS reverse proxy example
Dockerfile, docker-compose.yml, start_local.bat, start_mock_site.bat
data/                app.db, uploads, screenshots, pages, outputs (git-ignored, Docker volume)
```
