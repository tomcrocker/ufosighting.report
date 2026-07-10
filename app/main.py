import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

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
    return app
