from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class KnowledgeBaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("知识库名称不能为空")
        return value


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    description: str | None
    status: str
    document_count: int
    created_at: datetime | None
    updated_at: datetime | None


class KnowledgeBaseBatchDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    confirm_with_documents: bool = False


class KnowledgeBaseDeleteSkipped(BaseModel):
    id: str
    reason: str


class KnowledgeBaseBatchDeleteResponse(BaseModel):
    accepted: list[str]
    skipped: list[KnowledgeBaseDeleteSkipped]


class TaskRangeResponse(BaseModel):
    id: str
    task_no: int
    status: str
    completion_type: str | None
    chunk_count: int
    retry_count: int
    core_page_start: int
    core_page_end: int
    kind: str = "PARSE"
    attempt_count: int = 0
    max_attempts: int = 0
    dispatch_seq: int = 1
    last_error: str | None = None


class GenerationResponse(BaseModel):
    id: str
    version_no: int
    status: str
    expected_task_count: int
    completed_task_count: int
    failed_task_count: int
    canceled_task_count: int
    created_at: datetime | None
    finished_at: datetime | None
    chunk_count: int = 0
    published_at: datetime | None = None


class DocumentResponse(BaseModel):
    id: str
    knowledge_base_id: str
    filename: str
    file_size: int
    mime_type: str
    status: str
    support_open: bool = False
    current_generation_id: str | None
    building_generation_id: str | None
    current_run_id: str | None = None
    active_run_id: str | None = None
    created_at: datetime | None
    updated_at: datetime | None


class DocumentDetailResponse(DocumentResponse):
    current_generation: GenerationResponse | None = None
    building_generation: GenerationResponse | None = None
    latest_run: GenerationResponse | None = None


class ChunkResponse(BaseModel):
    chunk_id: str
    content: str
    chunk_index: int
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any]
    task_id: str | None = None
    generation_id: str | None = None
    relation_type: str | None = None


class SearchResponse(BaseModel):
    query: str
    current_generation_id: str | None = None
    active_run_ids: list[str] = Field(default_factory=list)
    results: list[dict[str, Any]]


class ModelProviderResponse(BaseModel):
    provider: str
    display_name: str
    configured: bool
    base_url: str
    api_key_masked: str | None
    chat_model: str | None
    embedding_model: str | None
    embedding_dimension: int | None
    capabilities: list[str]
    current_pipeline_compatible: bool


class ModelProviderTestRequest(BaseModel):
    text: str = Field(default="模型连接测试", min_length=1, max_length=200)


class ModelProviderTestResponse(BaseModel):
    provider: str
    success: bool
    message: str
    model: str | None = None
    vector_dimension: int | None = None
    chat_success: bool | None = None
    latency_ms: int | None = None


class ModelProviderCatalogResponse(BaseModel):
    provider_key: str
    display_name: str
    base_url: str
    chat_models: list[str]
    embedding_models: list[str]
    embedding_dimensions: dict[str, int] = Field(default_factory=dict)
    capabilities: list[str]


class ModelDiscoveryRequest(BaseModel):
    provider_key: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str | None = Field(default=None, max_length=2048)
    configuration_id: str | None = None


class DiscoveredModelResponse(BaseModel):
    id: str
    name: str
    description: str | None = None


class ModelDiscoveryResponse(BaseModel):
    chat_models: list[DiscoveredModelResponse]
    embedding_models: list[DiscoveredModelResponse]


class ModelConfigurationCreate(BaseModel):
    provider_key: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=1, max_length=2048)
    chat_model: str = Field(min_length=1, max_length=255)
    embedding_model: str | None = Field(default=None, max_length=255)
    embedding_dimension: int | None = Field(default=None, ge=1, le=100000)
    set_default: bool = False


class ModelConfigurationUpdate(BaseModel):
    provider_key: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str | None = Field(default=None, max_length=2048)
    chat_model: str = Field(min_length=1, max_length=255)
    embedding_model: str | None = Field(default=None, max_length=255)
    embedding_dimension: int | None = Field(default=None, ge=1, le=100000)
    set_default: bool = False


class ModelConfigurationResponse(BaseModel):
    id: str
    provider_key: str
    provider_name: str
    display_name: str
    base_url: str
    api_key_masked: str | None
    chat_model: str
    embedding_model: str | None
    embedding_dimension: int | None
    capabilities: list[str]
    is_default: bool
    created_at: datetime | None
    updated_at: datetime | None


class ModelConfigurationTestResponse(ModelProviderTestResponse):
    configuration_id: str
