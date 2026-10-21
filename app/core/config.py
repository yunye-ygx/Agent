from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the MySQL/MinIO/RabbitMQ/Elasticsearch demo."""

    app_name: str = "rag-demo"
    app_env: str = "local"
    log_level: str = "INFO"

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "rag_user"
    mysql_password: str = "rag_password"
    mysql_database: str = "rag_demo"

    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_username: str | None = None
    elasticsearch_password: str | None = None
    elasticsearch_index: str = "rag_demo_chunks_v1"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_files_bucket: str = "rag-demo-files"
    minio_artifacts_bucket: str = "rag-demo-artifacts"

    rabbitmq_url: str = "amqp://rag_user:rag_password@localhost:5672/"
    rabbitmq_exchange: str = "rag_demo.tasks"
    rabbitmq_routing_key: str = "task.execute"
    rabbitmq_queue: str = "rag_demo.task.execute"
    rabbitmq_dlx: str = "rag_demo.dlx"
    rabbitmq_dead_queue: str = "rag_demo.task.dead"

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int = 2048
    embedding_batch_size: int = 16
    model_timeout_seconds: float = 30.0
    llm_max_tokens: int = 512

    gpt_base_url: str = "https://api.openai.com/v1"
    gpt_api_key: str | None = None
    gpt_model: str | None = "gpt-4o-mini"
    gpt_embedding_model: str | None = "text-embedding-3-small"
    gpt_embedding_dimension: int = 1536

    mineru_command: str = "mineru"
    mineru_timeout_seconds: float = 120.0
    mineru_backend: str = "pipeline"
    mineru_method: str = "auto"
    chunking_temp_dir: str | None = None
    chunking_file_ttl_seconds: int = 3600
    chunking_max_file_bytes: int = 50 * 1024 * 1024
    chunking_max_tokens: int = 8192
    chunking_default_size: int = 500
    chunking_default_overlap: int = 80
    task_max_retry: int = 3
    task_page_size: int = 10
    task_lease_seconds: int = 15 * 60
    task_heartbeat_interval_seconds: float = 30.0
    task_execution_timeout_seconds: float = 30 * 60
    task_retry_backoff_seconds: float = 2.0
    dispatch_interval_seconds: float = 5.0
    maintenance_interval_seconds: float = 5.0
    worker_concurrency: int = 1
    worker_prefetch_count: int = 1
    es_batch_size: int = 100
    result_retention_seconds: int = 24 * 3600
    mineru_base_url: str | None = None
    mineru_api_version: str = "file_parse-v2"
    failed_generation_ttl_seconds: int = 24 * 3600
    run_background_services: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @computed_field
    @property
    def mysql_url(self) -> str:
        user = quote_plus(self.mysql_user)
        password = quote_plus(self.mysql_password)
        database = quote_plus(self.mysql_database)
        return f"mysql+asyncmy://{user}:{password}@{self.mysql_host}:{self.mysql_port}/{database}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
