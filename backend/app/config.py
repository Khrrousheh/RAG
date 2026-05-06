from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Policy RAG Chatbot API"
    api_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "company_policies_structural"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout_seconds: float = 240.0
    ollama_num_ctx: int = 4096
    ollama_num_predict: int = 700
    default_top_k: int = 5
    max_top_k: int = 10

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.api_cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
