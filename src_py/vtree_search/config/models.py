"""
목적:
- Vtree Search 라이브러리의 설정 인터페이스을 정의한다.

설명:
- Postgres/Redis/워커 제어 값을 단일 모델로 관리한다.
- LLM은 설정 파일이 아닌 Python 인자 주입으로 전달한다.

디자인 패턴:
- 값 객체(Value Object).

참조:
- scripts/run-search.py
- src_py/vtree_search/search/engine.py
- src_py/vtree_search/ingestion/ingestor.py
"""

from __future__ import annotations

from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator


class PostgresConfig(BaseModel):
    """Postgres 연결 및 테이블 설정 모델."""

    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = Field(min_length=1)
    password: str = Field(default="")
    database: str = Field(min_length=1)
    summary_table: str = Field(min_length=1)
    page_table: str = Field(min_length=1)
    embedding_dim: int = Field(ge=1)
    pool_min: int = Field(default=1, ge=1)
    pool_max: int = Field(default=8, ge=1)
    connect_timeout_ms: int = Field(default=2_000, ge=1)
    statement_timeout_ms: int = Field(default=3_000, ge=1)

    @field_validator("pool_max")
    @classmethod
    def validate_pool_max(cls, value: int, info) -> int:
        pool_min = info.data.get("pool_min", 1)
        if value < pool_min:
            raise ValueError("pool_max는 pool_min 이상이어야 합니다")
        return value

    def to_dsn(self) -> str:
        """Rust 계층 전달용 Postgres DSN을 생성한다."""
        user = quote(self.user, safe="")
        password = quote(self.password, safe="")
        host = self.host.strip()
        database = quote(self.database, safe="")
        return f"postgresql://{user}:{password}@{host}:{self.port}/{database}"


class RedisQueueConfig(BaseModel):
    """Redis Streams 큐 제어 설정 모델."""

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    username: str | None = Field(default=None)
    password: str | None = Field(default=None)
    use_ssl: bool = Field(default=False)
    module_name_search: str = Field(default="VtreeSearch", min_length=1)
    module_name_ingestion: str = Field(default="VtreeIngestor", min_length=1)
    stream_search: str = Field(default="search:jobs", min_length=1)
    stream_search_dlq: str = Field(default="search:jobs:dlq", min_length=1)
    consumer_group: str = Field(default="vtree-search-group", min_length=1)
    queue_max_len: int = Field(default=200, ge=1)
    queue_reject_at: int = Field(default=180, ge=1)
    result_ttl_sec: int = Field(default=900, ge=1)
    worker_block_ms: int = Field(default=1_000, ge=1)

    @field_validator("queue_reject_at")
    @classmethod
    def validate_reject_threshold(cls, value: int, info) -> int:
        queue_max_len = info.data.get("queue_max_len", 200)
        if value > queue_max_len:
            raise ValueError("queue_reject_at은 queue_max_len 이하이어야 합니다")
        return value


class SearchConfig(BaseModel):
    """검색 엔진 설정 모델."""

    postgres: PostgresConfig
    redis: RedisQueueConfig
    worker_concurrency: int = Field(default=4, ge=1)
    max_retries: int = Field(default=3, ge=0)
    retry_base_ms: int = Field(default=200, ge=1)
    retry_max_ms: int = Field(default=2_000, ge=1)
    entry_limit: int = Field(default=3, ge=1)
    page_limit: int = Field(default=50, ge=1)
    candidate_pool_factor: int = Field(default=3, ge=1)
    early_stop_min_entries: int = Field(default=1, ge=1)

    @field_validator("retry_max_ms")
    @classmethod
    def validate_retry_window(cls, value: int, info) -> int:
        retry_base_ms = info.data.get("retry_base_ms", 200)
        if value < retry_base_ms:
            raise ValueError("retry_max_ms는 retry_base_ms 이상이어야 합니다")
        return value

    @field_validator("early_stop_min_entries")
    @classmethod
    def validate_early_stop_min_entries(cls, value: int, info) -> int:
        entry_limit = info.data.get("entry_limit", 3)
        if value > entry_limit:
            raise ValueError("early_stop_min_entries는 entry_limit 이하이어야 합니다")
        return value


class IngestionPreprocessConfig(BaseModel):
    """파일 전처리/청킹 설정 모델."""

    max_chunk_chars: int = Field(default=1_024, ge=256)
    sample_per_extension: bool = Field(default=False)
    enable_table_annotation: bool = Field(default=True)
    enable_image_annotation: bool = Field(default=True)
    asset_output_dir: str = Field(default="data/ingestion-assets", min_length=1)


class SummaryStateConfig(BaseModel):
    """Redis 기반 summary_state 설정 모델."""

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    username: str | None = Field(default=None)
    password: str | None = Field(default=None)
    use_ssl: bool = Field(default=False)
    ttl_sec: int = Field(default=86_400, ge=60)
    lock_ttl_sec: int = Field(default=120, ge=5)


class IngestionConfig(BaseModel):
    """적재 엔진 설정 모델."""

    postgres: PostgresConfig
    preprocess: IngestionPreprocessConfig = Field(default_factory=IngestionPreprocessConfig)
    summary_state: SummaryStateConfig | None = Field(default=None)
