# Rust 모듈 레퍼런스

## 관련 문서

- [프로젝트 개요](../../README.md)
- [Rust 개요](./README.md)
- [아키텍처 청사진](../arch/blueprint.md)
- [Python 모듈 레퍼런스](../python/module_reference.md)
- [운영 가이드](../ops/queueing-and-slo.md)

## `src_rs/lib.rs`

- PyO3 모듈 엔트리: `_vtree_search`
- 노출 클래스: `SearchBridge`, `IngestionBridge`

## `src_rs/api/search_bridge.rs`

- 클래스: `PySearchBridge` (`pyclass name: SearchBridge`)
- 메서드:
  - `new()`
  - `status() -> String`
  - `execute(payload_json: &str) -> PyResult<String>`

## `src_rs/api/ingestion_bridge.rs`

- 클래스: `PyIngestionBridge` (`pyclass name: IngestionBridge`)
- 메서드:
  - `new()`
  - `status() -> String`
  - `execute(payload_json: &str) -> PyResult<String>`

## `src_rs/core/errors.rs`

- `CoreError`
  - `InvalidInput`
  - `InvalidConfig`
  - `Db`
  - `Serialization`
  - `Runtime`

## `src_rs/core/search_pipeline.rs`

- 입력: `SearchRequestPayload`
  - `candidate_pool_factor`
  - `early_stop_min_entries`
  - Postgres 연결/테이블 정보
- 출력: `SearchResultPayload` (LLM 필터 전 후보 + 메트릭)
- 함수: `execute_search(payload)`

## `src_rs/core/ingestion_pipeline.rs`

- 입력: `IngestionRequestPayload`
  - `operation=upsert_document|upsert_pages|rebuild_summary_embeddings`
  - summary/page 노드 + Postgres 연결 정보
- 출력: `IngestionResultPayload`
- 함수: `execute_ingestion(payload)`

## `src_rs/index/postgres_repo.rs`

- `PostgresRepository`
- 함수:
  - `search_summary_nodes()`
  - `fetch_pages_under_path()`
  - `upsert_summary_nodes()`
  - `upsert_page_nodes()`
  - `touch_summary_nodes()`

## `src_rs/index/sql.rs`

- `validate_identifier(value, field_name)`
- `to_pgvector_literal(values)`
