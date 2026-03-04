# Python 계층 개요

## 관련 문서

- [프로젝트 개요](../../README.md)
- [아키텍처 청사진](../arch/blueprint.md)
- [런타임 동작](../arch/how-this-works.md)
- [Python LLM 주입](./llm_injection.md)
- [Python 모듈 레퍼런스](./module_reference.md)
- [Rust 개요](../rust/README.md)
- [운영 가이드](../ops/queueing-and-slo.md)

## 목적

`src_py/vtree_search`의 공개 라이브러리 경계를 정의한다.

## 핵심 클래스

- `VtreeIngestor`
  - 문서 파싱, 요약 상태 누적, 적재 실행
- `VTreeSearchEngine`
  - 잡 제출/워커 실행/상태 조회/결과 조회/취소

## 필수 모듈

- `config/`: 설정 모델
- `contracts/`: DTO/잡 상태 모델
- `runtime/`: Rust 브릿지 래퍼
- `queue/`: Redis Streams 제어
- `llm/`: LangChain 어댑터
- `ingestion/`: 적재 서비스 + `summary_state` + `prompts/`
- `search/`: 검색 서비스

## Ingestion 멀티모달 처리 모듈

- `ingestion/source_parser.py`
  - Markdown/PDF/DOCX 파싱
  - Markdown 헤딩 추출 + 블록 변환
  - PDF 표/이미지 주석 본문 생성
- `ingestion/docx_layout.py`
  - DOCX 레이아웃 기반 페이지 추정(A4 기준)
- `ingestion/parser_helpers.py`
  - 표 HTML 직렬화
  - DOCX 제목 레벨 추정
  - PDF 이미지 bbox -> 픽셀 변환
  - 블록 청킹(`heading` 경계 포함)
- `ingestion/summary_state.py`
  - Redis writer lock
  - `meta/page_done/subch/ch/doc` 누적 상태 관리

## Ingestion 부하 제어 포인트

- `IngestionPreprocessConfig.sample_per_extension`
- `IngestionPreprocessConfig.max_chunk_chars`
- `IngestionPreprocessConfig.enable_table_annotation`
- `IngestionPreprocessConfig.enable_image_annotation`
- `IngestionPreprocessConfig.asset_output_dir`
- `SummaryStateConfig.ttl_sec`
- `SummaryStateConfig.lock_ttl_sec`

## 책임 분리

### 라이브러리 책임

- Rust 실행 경로 연결
- 잡 큐잉/재시도/DLQ
- 적재 중간 상태(`summary_state`) 누적
- LLM 호출 형식 강제

### 소비자 앱 책임

- HTTP API 구현
- 인증/인가
- 멀티테넌시
- 요청 라우팅
- 실제 모델/키 관리

## `.env` 원칙

- 라이브러리 내부에서 `.env`를 읽지 않는다.
- `scripts/run-search.py`, `scripts/run-ingestion.py`가 `.env`를 읽어 설정을 주입한다.
- Postgres 키:
  - `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DATABASE`
  - `VTREE_SUMMARY_TABLE`, `VTREE_PAGE_TABLE`, `VTREE_EMBEDDING_DIM`
  - `POSTGRES_POOL_MIN`, `POSTGRES_POOL_MAX`, `POSTGRES_CONNECT_TIMEOUT_MS`, `POSTGRES_STATEMENT_TIMEOUT_MS`
- Redis 키:
  - `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_USERNAME`, `REDIS_PASSWORD`, `REDIS_USE_SSL`
  - `REDIS_STREAM_SEARCH`, `REDIS_STREAM_SEARCH_DLQ`, `REDIS_CONSUMER_GROUP`
  - `REDIS_MODULE_SEARCH`, `REDIS_MODULE_INGESTION`
  - 현재 검색 잡 상태 해시에는 `REDIS_MODULE_SEARCH` 값이 기록된다.
- 검색 제어 키:
  - `QUEUE_MAX_LEN`, `QUEUE_REJECT_AT`, `JOB_RESULT_TTL_SEC`, `WORKER_BLOCK_MS`
  - `WORKER_CONCURRENCY`, `JOB_MAX_RETRIES`, `JOB_RETRY_BASE_MS`, `JOB_RETRY_MAX_MS`
  - `SEARCH_CANDIDATE_POOL_FACTOR`, `SEARCH_EARLY_STOP_MIN_ENTRIES`
- 적재 제어 키:
  - `INGEST_MAX_CHUNK_CHARS`, `INGEST_SAMPLE_PER_EXTENSION`
  - `INGEST_ENABLE_TABLE_ANNOTATION`, `INGEST_ENABLE_IMAGE_ANNOTATION`
  - `INGEST_ASSET_OUTPUT_DIR`, `SUMMARY_STATE_TTL_SEC`, `SUMMARY_STATE_LOCK_TTL_SEC`
- Gemini 기준 키:
  - `GOOGLE_API_KEY`
  - `SEARCH_LLM_MODEL`
  - `INGESTION_LLM_MODEL`
- `temperature`는 드라이버 코드 고정값(`0.0`)이다.
