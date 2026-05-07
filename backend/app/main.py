import logging
from contextlib import asynccontextmanager

import anyio
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .config import Settings, get_settings
from .rag import RagService, get_rag_service
from .schemas import (
    ChatRequest,
    ChatResponse,
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

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=HealthResponse)
    async def health(service: RagService = Depends(get_rag_service)) -> HealthResponse:
        return HealthResponse(**await service.health_async())

    @app.get("/metadata", response_model=MetadataResponse)
    async def metadata(service: RagService = Depends(get_rag_service)) -> MetadataResponse:
        return MetadataResponse(**await service.metadata_async())

    @app.post("/search")
    async def search(
        request: SearchRequest,
        service: RagService = Depends(get_rag_service),
    ) -> dict[str, object]:
        return {"sources": await service.search_async(request)}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        request: ChatRequest,
        service: RagService = Depends(get_rag_service),
        settings: Settings = Depends(get_settings),
    ) -> ChatResponse:
        request.top_k = min(request.top_k, settings.max_top_k)
        answer, sources, warnings = await service.chat_async(request)
        return ChatResponse(answer=answer, sources=sources, warnings=warnings)

    @app.post("/chat/stream")
    async def chat_stream(
        request: ChatRequest,
        service: RagService = Depends(get_rag_service),
        settings: Settings = Depends(get_settings),
    ) -> StreamingResponse:
        request.top_k = min(request.top_k, settings.max_top_k)
        return StreamingResponse(
            service.stream_chat(request),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
