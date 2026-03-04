# Vtree Search 런타임 동작 문서

## 관련 문서

- [프로젝트 개요](../../README.md)
- [아키텍처 청사진](./blueprint.md)
- [이론 배경](./theoretical_background.md)
- [운영 가이드](../ops/queueing-and-slo.md)
- [Python LLM 주입](../python/llm_injection.md)

## 1. 개요

본 문서는 라이브러리 모드에서 실제 동작하는 실행 경로를 설명한다.

- 공개 클래스: `VtreeIngestor`, `VTreeSearchEngine`
- 큐: Redis Streams
- DB: PostgreSQL (`pgvector`, `ltree`)
- LLM: Python 주입 LangChain 객체(`ainvoke`)
- 적재 중간 상태: Redis `summary_state`

## 2. 검색 실행 시퀀스

1. `submit_search(query_text, query_embedding, top_k)` 호출
2. fingerprint 계산 후 중복 검사
   - 동일 요청이 `PENDING`/`RUNNING`/`SUCCEEDED`면 기존 job 재사용
3. 큐 포화 검사
   - `depth >= QUEUE_REJECT_AT`면 즉시 거절
4. `job:{id}` 상태 해시 생성(`PENDING`)
   - `module_name` 필드에는 `REDIS_MODULE_SEARCH` 값이 기록
5. Stream(`REDIS_STREAM_SEARCH`)에 메시지 enqueue
6. 워커(`run_worker_once`/`run_worker_forever`)가 메시지 소비
7. 상태 `RUNNING` 전환 후 Rust 검색 파이프라인 실행
8. Rust가 summary 조회 + page 확장을 수행
   - 후보 dedupe
   - 조기 종료(`candidate_pool_factor`, `early_stop_min_entries`)
9. Python이 LangChain 배치 필터(`ainvoke`) 적용 후 top-k 절삭
10. 성공 시 `SUCCEEDED` + `result_json` 저장 + ACK
11. 실패 시 재시도 또는 DLQ(`REDIS_STREAM_SEARCH_DLQ`) 이동 후 ACK

## 3. 적재 실행 시퀀스 (`upsert_document_from_path`)

1. 입력 검증
   - `summary_nodes`가 비어 있으면 실패
2. `summary_state` writer lock 획득
   - 키: `sumstate:{document_id}:{version}:lock`
3. SourceParser 실행
   - Markdown/PDF/DOCX 블록 추출
   - PDF 표/이미지, DOCX 표 LLM 주석
4. 헤딩 기반 sub-ch 생성
   - Markdown 헤딩/문단 메타데이터 기반 그룹화
   - `sub-ch/ch/doc` 제목 생성
5. `summary_state` 단계 기록
   - `initialize_run(PAGE)`
   - `page_done` 업데이트
   - `subch/ch/doc(DRAFT)` revision 기반 upsert
6. Rust 적재 실행
   - `upsert_document`로 summary/page 업서트
7. 완료 상태 전이
   - 성공: `doc=FINAL`, `meta=SUCCEEDED`
   - 실패: `meta=FAILED`
8. lock 해제

## 4. 상태 머신

### 검색 job 상태

- `PENDING -> RUNNING -> SUCCEEDED`
- `PENDING -> RUNNING -> FAILED`
- `PENDING/RUNNING -> CANCELED`

### 적재 run 상태(`summary_state.meta.status`)

- `RUNNING -> SUCCEEDED`
- `RUNNING -> FAILED`

## 5. 운영 지표

필수 수집 지표:

- `queue_depth`
- `queue_reject_count`
- `job_retry_count`
- `dlq_count`
- `job_latency_ms`
- `db_query_latency_ms`
- `search_llm_filter_latency_ms`
- `search_llm_filter_error_rate`
- `ingestion_llm_annotation_latency_ms`
- `ingestion_llm_annotation_error_rate`
- `summary_state_failed_runs`
- `summary_state_lock_acquire_failures`

## 6. 서비스 레벨 목표(초기값)

- 제출 응답 p95 <= 500ms
- 제출~완료 p95 <= 4s
- 큐 거절 비율 < 2%

## 7. 예외/오류 전달

- 포화: `QueueOverloadedError`
- 잡 없음: `JobNotFoundError`
- TTL 만료: `JobExpiredError`
- 실행 실패: `JobFailedError`
- 의존성 없음: `DependencyUnavailableError`
- 적재 파싱/주석 실패: `IngestionProcessingError`
