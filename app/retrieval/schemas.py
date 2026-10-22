from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    query: str = Field(min_length=1, max_length=4096)
    knowledge_base_id: str = Field(min_length=1, max_length=64)
    document_id: str | None = Field(default=None, min_length=1, max_length=64)
    top_k: int = Field(default=50, ge=1, le=500)
    top_n: int = Field(default=5, ge=1, le=50)
    threshold: float = Field(default=0.2, ge=-1, le=1)
    model_weight: float = Field(default=0.7, ge=0, le=1)
    title_weight: float = Field(default=2, ge=0, le=100)
    content_weight: float = Field(default=1, ge=0, le=100)
    scoring_mode: Literal["auto", "vector", "rerank"] = "auto"
    rerank_config_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("query", "knowledge_base_id", "document_id", "rerank_config_id")
    @classmethod
    def nonblank(cls, value):
        if value is None:
            return value
        if not value.strip():
            raise ValueError("不能为空白")
        return value.strip()

    @model_validator(mode="after")
    def relationships(self):
        if self.top_n > self.top_k:
            raise ValueError("Top N 不能大于 Top K")
        if self.title_weight + self.content_weight <= 0:
            raise ValueError("标题与正文权重之和必须大于零")
        return self
