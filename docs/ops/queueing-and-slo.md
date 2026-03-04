# Queueing & SLO 운영 가이드

## 관련 문서

- [프로젝트 개요](../../README.md)
- [아키텍처 청사진](../arch/blueprint.md)
- [런타임 동작](../arch/how-this-works.md)
- [이론 배경](../arch/theoretical_background.md)
- [Python 개요](../python/README.md)
- [Python LLM 주입](../python/llm_injection.md)

## 1. 검색 큐 정책

- 모델: Redis Streams + Consumer Group
- 보장: At-least-once
- 상태 저장: `job:{id}` 해시 + TTL
- DLQ: `search:jobs:dlq`
- dedupe: `query_text + query_embedding + top_k + metadata` fingerprint

## 2. 임계치 기본값

- `QUEUE_MAX_LEN=200`
- `QUEUE_REJECT_AT=180`
- `WORKER_CONCURRENCY=4`
- `JOB_MAX_RETRIES=3`
- `JOB_RETRY_BASE_MS=200`
- `JOB_RETRY_MAX_MS=2000`
- `SEARCH_CANDIDATE_POOL_FACTOR=3`
- `SEARCH_EARLY_STOP_MIN_ENTRIES=1`

## 3. 키-동작 매핑

- `REDIS_STREAM_SEARCH`: 검색 큐 Stream 이름
- `REDIS_STREAM_SEARCH_DLQ`: 검색 실패 DLQ Stream 이름
- `REDIS_CONSUMER_GROUP`: 검색 워커 Consumer Group 이름
- `REDIS_MODULE_SEARCH`: `job:{id}`의 `module_name` 필드 값
- `REDIS_MODULE_INGESTION`: 적재 모듈 태그(현재 적재 큐 미사용 상태에서 예약 키)
- `VTREE_SUMMARY_TABLE`: summary 업서트 대상 테이블
- `VTREE_PAGE_TABLE`: page 업서트 대상 테이블

## 4. SLO 목표

- 제출 응답 p95 <= 500ms
- 제출~완료 p95 <= 4s
- 큐 거절 비율 < 2%

## 5. 검색 장애 대응

### 큐 적체 증가

1. `queue_depth`, `queue_reject_count` 확인
2. `search_llm_filter_latency_ms` 확인
3. 필요 시 `QUEUE_REJECT_AT` 하향 또는 워커 수평 확장

### DLQ 증가

1. `dlq_count` 확인
2. `last_error` 패턴 분석
3. 원인 수정 후 DLQ 재처리 배치 실행

### DB 지연

1. `db_query_latency_ms` 확인
2. 인덱스 상태(`pgvector`, `ltree`) 점검
3. `statement_timeout_ms`, `pool_min/max` 재조정

## 6. Ingestion 부하 제어

### 주석 비용 제어

1. 샘플 모드(`INGEST_SAMPLE_PER_EXTENSION=true`)로 유형별 비용 측정
2. `INGEST_ENABLE_TABLE_ANNOTATION`, `INGEST_ENABLE_IMAGE_ANNOTATION` 분리 제어
3. `INGEST_MAX_CHUNK_CHARS` 과대 설정 방지
4. `INGEST_ASSET_OUTPUT_DIR` 디스크 사용량 점검

### summary_state 운영 제어

1. 동일 문서 버전 동시 쓰기 방지
   - writer lock: `sumstate:{document_id}:{version}:lock`
2. 보관 정책
   - `SUMMARY_STATE_TTL_SEC`
3. lock TTL
   - `SUMMARY_STATE_LOCK_TTL_SEC`
4. 실패 복구 기준
   - `meta.status=FAILED` run 추적 후 재실행

### 권장 운영 지표

- `ingestion_files_total`
- `ingestion_tables_total`
- `ingestion_images_total`
- `ingestion_llm_annotation_latency_ms`
- `ingestion_llm_annotation_error_rate`
- `ingestion_asset_disk_usage_bytes`
- `summary_state_failed_runs`
- `summary_state_lock_acquire_failures`

## 7. Ingestion 장애 대응

### 주석 지연/실패 증가

1. `ingestion_llm_annotation_latency_ms`, `ingestion_llm_annotation_error_rate` 확인
2. 표/이미지 플래그 분리 적용으로 병목 모듈 분리
3. 샘플 모드 축소 실행으로 원인 문서 유형(PDF/DOCX) 특정

### summary_state lock 실패 증가

1. 동일 `document_id + version` 중복 실행 여부 확인
2. lock TTL(`SUMMARY_STATE_LOCK_TTL_SEC`) 과도 단축 여부 확인
3. 장시간 작업 문서의 chunk 전략/입력 분할 검토

### 이미지 자산 급증

1. `INGEST_ASSET_OUTPUT_DIR` 용량 추세 확인
2. 보존 기간/정리 주기 명시
3. 입력 문서 품질(과도한 이미지 반복) 점검
