import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    app = FastAPI(title=settings.app_name)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health(service: RagService = Depends(get_rag_service)) -> HealthResponse:
        return HealthResponse(**service.health())

    @app.get("/metadata", response_model=MetadataResponse)
    def metadata(service: RagService = Depends(get_rag_service)) -> MetadataResponse:
        return MetadataResponse(**service.metadata())

    @app.post("/search")
    def search(
        request: SearchRequest,
        service: RagService = Depends(get_rag_service),
    ) -> dict[str, object]:
        return {"sources": service.search(request)}

    @app.post("/chat", response_model=ChatResponse)
    def chat(
        request: ChatRequest,
        service: RagService = Depends(get_rag_service),
        settings: Settings = Depends(get_settings),
    ) -> ChatResponse:
        request.top_k = min(request.top_k, settings.max_top_k)
        answer, sources, warnings = service.chat(request)
        return ChatResponse(answer=answer, sources=sources, warnings=warnings)

    return app


app = create_app()
