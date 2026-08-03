"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from spirrow_conclair import __version__
from spirrow_conclair.api import (
    events_router,
    integrity_router,
    messages_router,
    projects_router,
    read_cursor_router,
    threads_router,
)
from spirrow_conclair.api.error_handlers import register_error_handlers
from spirrow_conclair.config import Settings, get_settings
from spirrow_conclair.db import dispose_db, health_check, init_db
from spirrow_conclair.web import router as ui_router
from spirrow_conclair.web.deps import set_templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    init_db(settings)
    try:
        yield
    finally:
        await dispose_db()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="spirrow-conclair",
        description="Chatroom persistence backend (FastAPI + PostgreSQL)",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

    register_error_handlers(app)
    app.include_router(threads_router)
    app.include_router(messages_router)
    app.include_router(events_router)
    app.include_router(integrity_router)
    app.include_router(read_cursor_router)
    app.include_router(projects_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    set_templates(Jinja2Templates(directory=str(TEMPLATES_DIR)))
    app.include_router(ui_router)

    @app.get("/health")
    async def health() -> JSONResponse:
        db_ok = await health_check()
        if db_ok:
            return JSONResponse(
                {"status": "healthy", "db": "ok", "version": __version__},
                status_code=200,
            )
        return JSONResponse(
            {"status": "degraded", "db": "error", "version": __version__},
            status_code=503,
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "spirrow_conclair.main:app",
        host="127.0.0.1",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
