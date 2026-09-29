"""Extension API (newplan section 5, step A): the browser extension's only way into the app.

    python -m app.api                 (http://127.0.0.1:8000, docs at /docs)

Every /api endpoint except /api/health needs `Authorization: Bearer <token>` (tokens are created
on the dashboard's "Extension tokens" page). CORS is limited to ALLOWED_EXTENSION_ORIGINS.
The Gemini key never leaves the server: the extension only uploads what the page showed.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import lookups
from app.core.config import SiteProfile, Settings, get_settings
from app.core.db import Database
from app.core.logging_setup import setup_logging
from app.core.storage import LocalStorage
from app.jobs import export_final
from app.lookups import LookupRequestError

log = logging.getLogger("app.api")


class CaptureIn(BaseModel):
    text: str | None = None
    screenshot_b64: str | None = None
    url: str | None = Field(None, max_length=2000)
    recapture: bool = False


class StatusIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=300)


class ReleaseIn(BaseModel):
    pnrs: list[str] | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    storage = LocalStorage(settings.data_dir)
    Database(settings.db_path).close()  # apply migrations once at start-up

    app = FastAPI(title="GDS PNR Lookup: extension API", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=settings.extension_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(LookupRequestError)
    async def _lookup_error(_request: Request, exc: LookupRequestError):
        return JSONResponse(status_code=exc.code, content={"detail": str(exc)})

    def get_db() -> Iterator[Database]:
        db = Database(settings.db_path)  # one connection per request: requests run in threads
        try:
            yield db
        finally:
            db.close()

    def auth(authorization: str | None = Header(None), db: Database = Depends(get_db)):
        token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        row = lookups.authenticate(db, token)
        if row is None:
            raise LookupRequestError("missing, wrong or revoked token", 401)
        return row

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/me")
    def me(token=Depends(auth)):
        return {"name": token["name"]}

    def own_job(db: Database, job_id: int, token):
        """The job, only if it belongs to this token's user (else 404, so users can't probe others)."""
        job = db.get_job(job_id)
        if job is None or job["user_id"] != token["user_id"]:
            raise LookupRequestError("job not found", 404)
        return job

    @app.get("/api/jobs")
    def jobs(token=Depends(auth), db: Database = Depends(get_db)):
        return lookups.open_jobs(db, token["user_id"])

    @app.get("/api/site-profiles")
    def site_profiles(token=Depends(auth), db: Database = Depends(get_db)):
        out = []
        for name in lookups.site_profile_names(db):
            try:
                p = lookups.get_site_profile(db, name)
                out.append({"name": name, "title": p.name, "configured": not p.is_placeholder})
            except (LookupRequestError, ValueError) as exc:
                out.append({"name": name, "title": name, "configured": False, "error": str(exc)[:200]})
        return out

    @app.get("/api/site-profile/{name}")
    def site_profile(name: str, token=Depends(auth), db: Database = Depends(get_db)):
        p = lookups.get_site_profile(db, name)
        return {**p.model_dump(), "configured": not p.is_placeholder}

    @app.put("/api/site-profile/{name}")
    def put_site_profile(name: str, profile: SiteProfile, token=Depends(auth), db: Database = Depends(get_db)):
        lookups.save_site_profile(db, name, profile)
        return {**profile.model_dump(), "configured": not profile.is_placeholder}

    @app.post("/api/jobs/{job_id}/claim")
    def claim(job_id: int, n: int = Query(20, ge=1, le=100), token=Depends(auth), db: Database = Depends(get_db)):
        own_job(db, job_id, token)
        return {"leases": lookups.claim(db, job_id, token, n, settings)}

    @app.post("/api/jobs/{job_id}/lookups/{pnr}/capture")
    def capture(job_id: int, pnr: str, body: CaptureIn, token=Depends(auth), db: Database = Depends(get_db)):
        own_job(db, job_id, token)
        res = lookups.save_capture(db, storage, settings, job_id, pnr, token, text=body.text,
                                   screenshot_b64=body.screenshot_b64, url=body.url, recapture=body.recapture)
        return {"status": res.status, "stored": res.stored}

    @app.post("/api/jobs/{job_id}/lookups/{pnr}/status")
    def status(job_id: int, pnr: str, body: StatusIn, token=Depends(auth), db: Database = Depends(get_db)):
        own_job(db, job_id, token)
        return {"status": lookups.set_status(db, job_id, pnr, token, body.status, body.note)}

    @app.post("/api/jobs/{job_id}/release")
    def release(job_id: int, body: ReleaseIn | None = None, token=Depends(auth), db: Database = Depends(get_db)):
        own_job(db, job_id, token)
        return {"released": lookups.release(db, job_id, token, body.pnrs if body else None)}

    @app.post("/api/jobs/{job_id}/resume")
    def resume(job_id: int, token=Depends(auth), db: Database = Depends(get_db)):
        """After a BLOCKED page was dealt with in the browser: re-open the paused job."""
        own_job(db, job_id, token)
        if not lookups.resume(db, job_id):
            raise LookupRequestError("job is not paused", 409)
        return {"status": db.get_job(job_id)["status"]}

    @app.get("/api/jobs/{job_id}/final.xlsx")
    def final_excel(job_id: int, token=Depends(auth), db: Database = Depends(get_db)):
        """The final Excel as it is right now (built fresh)."""
        own_job(db, job_id, token)
        xlsx_key, _ = export_final(db, storage, job_id)
        return FileResponse(storage.path(xlsx_key), filename=f"job_{job_id}_final.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/jobs/{job_id}/progress")
    def progress(job_id: int, token=Depends(auth), db: Database = Depends(get_db)):
        own_job(db, job_id, token)
        return lookups.progress(db, job_id)

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    if not settings.extension_origins:
        log.warning("ALLOWED_EXTENSION_ORIGINS is empty: browsers will refuse cross-origin calls from web pages. "
                    "The extension itself works through its host permission.")
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port, log_level="warning")


if __name__ == "__main__":
    main()
