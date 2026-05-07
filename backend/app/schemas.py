from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=6000)


class ChatRequest(BaseModel):
    session_id: UUID | None = None
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
    session: "ChatSessionResponse | None" = None
    memory: "MemoryUsageResponse | None" = None


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
    postgres: str | None = None
    redis: str | None = None
    warnings: list[str] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=160)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None = None
    email: str
    display_name: str | None = None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)


class SessionUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=180)


class ChatSessionResponse(BaseModel):
    id: UUID
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None


class ConversationTurnResponse(BaseModel):
    id: UUID
    session_id: UUID
    sequence: int
    role: str
    content: str
    sources: list[dict[str, object]] | None = None
    warnings: list[str] | None = None
    metrics: dict[str, object] | None = None
    created_at: datetime


class SessionMessagesResponse(BaseModel):
    session: ChatSessionResponse
    messages: list[ConversationTurnResponse]


class MemoryUsageResponse(BaseModel):
    short_term_turns: int = 0
    long_term_memories: int = 0
    summary_enqueued: bool = False


class SessionStreamEvent(BaseModel):
    event: str = "session"
    session: ChatSessionResponse


ChatResponse.model_rebuild()
