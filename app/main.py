import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.config import get_settings


def create_app(start_thumb_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(get_settings().db_path)
        db.init_db(conn)
        conn.close()
        stop_event = threading.Event()
        worker = None
        if start_thumb_worker:
            from app import thumbs

            worker = thumbs.start_worker(stop_event)
        yield
        stop_event.set()
        if worker:
            worker.join(timeout=15)

    app = FastAPI(title="ufosighting.report", lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent.parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from app.routes import auth as auth_routes

    app.include_router(auth_routes.router)

    from app.routes import submit as submit_routes

    app.include_router(submit_routes.router)

    from app.routes import public as public_routes

    app.include_router(public_routes.router)

    from app.routes import verify as verify_routes

    app.include_router(verify_routes.router)

    from app.routes import admin as admin_routes

    app.include_router(admin_routes.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def html_errors(request: Request, exc: StarletteHTTPException):
        # API callers keep JSON; humans get a page instead of {"detail": ...}
        from fastapi.responses import JSONResponse

        from app.web import templates
        if request.url.path.startswith("/api/") or exc.status_code < 400:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        if exc.status_code in (301, 302, 303, 307, 308):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        return templates.TemplateResponse(
            request, "error.html", {"user": None, "code": exc.status_code},
            status_code=exc.status_code)

    return app
