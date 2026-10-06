"""Application entry point: FastAPI backend + NiceGUI (Python-driven) frontend in one process.

Run:  python -m app.main            (see scripts/run.ps1)
"""
from __future__ import annotations

import logging
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import api, db, embeddings, indexing, ingestion, profiles, security
from .config import Settings, load_settings, set_current

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

ERROR_MAP = {
    security.AuthError: 401,
    security.PermissionDenied: 403,
    LookupError: 404,
    security.QuotaExceeded: 429,
    ingestion.IngestError: 400,
    ValueError: 400,
}


def bootstrap(settings: Settings) -> None:
    set_current(settings)
    db.init_db(settings.db_path)
    profiles.seed_examples(settings.extra.get("embedding") or embeddings.default_config())
    security.ensure_admin(settings)
    indexing.ensure_active_version()
    # resume documents that were queued when the app stopped (crash / reboot recovery)
    for d in db.query("SELECT id FROM documents WHERE status = 'queued' AND deleted_at IS NULL"):
        threading.Thread(target=indexing.process_document, args=(d["id"], settings), daemon=True).start()


def create_app(settings: Settings | None = None, with_ui: bool = True) -> FastAPI:
    settings = settings or load_settings()
    bootstrap(settings)
    app = FastAPI(title="Document Knowledge Assistant (POC)", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(api.router)

    for exc_type, status in ERROR_MAP.items():
        def handler(request: Request, exc: Exception, status=status):
            return JSONResponse({"detail": str(exc)}, status_code=status)
        app.add_exception_handler(exc_type, handler)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.https_only_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/healthz")
    def healthz():
        db.one("SELECT 1 AS ok")
        return {"status": "ok", "index_version": (indexing.active_version() or {}).get("id")}

    if with_ui:
        from . import ui
        ui.setup(app, settings)
    return app


def main() -> None:
    import os

    import uvicorn

    try:
        app = create_app()
    except RuntimeError as exc:  # configuration problems: one clear line instead of a traceback
        raise SystemExit(f"\nSTARTUP ERROR: {exc}\n") from None
    host = os.getenv("HOST", "127.0.0.1")  # localhost only: remote access comes through the AWS tunnel
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ in {"__main__", "__mp_main__"}:
    main()
