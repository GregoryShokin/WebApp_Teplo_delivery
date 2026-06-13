from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.jobs.scheduler import get_scheduler
from app.services import position_registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        async with AsyncSessionLocal() as session:
            await position_registry.refresh_position_registry(session)
    except Exception:  # noqa: BLE001 - без БД работаем на встроенном каноне
        pass
    scheduler = get_scheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.backend_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def refresh_position_registry_middleware(request: Request, call_next) -> Response:
        # Реестр должностей читается из кэш-снапшота; освежаем по TTL, чтобы
        # изменения подхватывались во всех воркерах, а не только в том, где CRUD.
        if position_registry.registry_is_stale():
            try:
                async with AsyncSessionLocal() as session:
                    await position_registry.refresh_position_registry(session)
            except Exception:  # noqa: BLE001 - не валим запрос из-за реестра
                pass
        return await call_next(request)

    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
