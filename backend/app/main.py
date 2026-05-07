import logging
from contextlib import asynccontextmanager

import anyio
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api_auth import router as auth_router
from .api_chat import router as chat_router
from .config import get_settings
from .db import check_db, dispose_db
from .rag import RagService, get_rag_service
from .redis_client import check_redis, close_redis
from .seed import seed_default_user
from .schemas import (
    HealthResponse,
    MetadataResponse,
    SearchRequest,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service = get_rag_service()
        await seed_default_user(settings)
        await service.startup()
        if settings.warm_embeddings_on_startup:
            await anyio.to_thread.run_sync(service.warm_embeddings)
        if settings.warm_metadata_on_startup:
            await anyio.to_thread.run_sync(service.warm_metadata)
        if settings.warm_llm_on_startup:
            await service.warm_llm_async()
        try:
            yield
        finally:
            await service.shutdown()
            await close_redis()
            await dispose_db()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    app.include_router(chat_router)

    @app.get("/health", response_model=HealthResponse)
    async def health(service: RagService = Depends(get_rag_service)) -> HealthResponse:
        health_data = await service.health_async()
        postgres_status = await check_db()
        redis_status = await check_redis()
        warnings = list(health_data.get("warnings") or [])
        if postgres_status != "ok":
            warnings.append("Postgres check failed.")
        if redis_status != "ok":
            warnings.append("Redis check failed.")
        if postgres_status != "ok" or redis_status != "ok":
            health_data["status"] = "degraded"
        health_data.pop("warnings", None)
        return HealthResponse(
            **health_data,
            postgres=postgres_status,
            redis=redis_status,
            warnings=warnings,
        )

    @app.get("/metadata", response_model=MetadataResponse)
    async def metadata(service: RagService = Depends(get_rag_service)) -> MetadataResponse:
        return MetadataResponse(**await service.metadata_async())

    @app.post("/search")
    async def search(
        request: SearchRequest,
        service: RagService = Depends(get_rag_service),
    ) -> dict[str, object]:
        return {"sources": await service.search_async(request)}

    return app


app = create_app()
