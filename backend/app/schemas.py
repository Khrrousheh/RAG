from datetime import date

from pydantic import BaseModel, Field


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=6000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    policy: str | None = None
    department: str | None = None
    version: str | None = None
    effective_date_from: date | None = None
    effective_date_to: date | None = None
    top_k: int = Field(default=5, ge=1, le=10)
    use_llm: bool = True
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)


class Source(BaseModel):
    id: int
    score: float
    policy_name: str | None = None
    file_name: str | None = None
    page: int | None = None
    department: str | None = None
    version: str | None = None
    effective_date: str | None = None
    policy_title: str | None = None
    text: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]
    warnings: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    policy: str | None = None
    department: str | None = None
    version: str | None = None
    effective_date_from: date | None = None
    effective_date_to: date | None = None
    top_k: int = Field(default=5, ge=1, le=10)


class MetadataResponse(BaseModel):
    departments: list[str]
    versions: list[str]
    policies: list[str]


class HealthResponse(BaseModel):
    status: str
    qdrant: str
    ollama: str
    collection: str
    points_count: int | None = None
    warnings: list[str] = Field(default_factory=list)
