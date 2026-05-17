from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Policy RAG Chatbot API"
    api_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "company_policies_structural"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    ollama_base_url: str = "http://localhost:12434"
    ollama_model: str = "ai/gemma3-qat"
    ollama_timeout_seconds: float = 240.0
    ollama_num_ctx: int = 4096
    ollama_num_predict: int = 256
    ollama_keep_alive: str = "30m"
    default_top_k: int = 5
    max_top_k: int = 10
    warm_embeddings_on_startup: bool = True
    warm_llm_on_startup: bool = True
    warm_metadata_on_startup: bool = True
    embedding_cache_size: int = 256
    prompt_context_max_chars: int = 2800
    prompt_min_sources: int = 3
    prompt_max_sources: int = 4
    http_max_connections: int = 20
    http_max_keepalive_connections: int = 10
    database_url: str = "postgresql+asyncpg://rag:rag_password@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"
    jwt_access_secret: str = "change-me-access-secret"
    jwt_refresh_secret: str = "change-me-refresh-secret"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    refresh_cookie_name: str = "rag_refresh_token"
    refresh_cookie_secure: bool = False
    refresh_cookie_samesite: str = "lax"
    auth_login_rate_limit: int = 12
    auth_login_rate_window_seconds: int = 300
    short_term_memory_turns: int = 16
    short_term_memory_ttl_seconds: int = 604800
    long_term_memory_top_k: int = 5
    qdrant_memory_collection: str = "user_memories"
    memory_summary_turn_threshold: int = 8
    memory_summary_char_threshold: int = 12000
    memory_job_queue: str = "memory_jobs"
    memory_worker_poll_timeout_seconds: int = 5
    memory_worker_max_attempts: int = 3
    seed_default_user: bool = True
    default_user_login: str = "mahdi"
    default_user_password: str = "123456"
    default_user_display_name: str = "Mahdi"

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
