# Vtree Search 아키텍처 청사진

## 관련 문서

- [프로젝트 개요](../../README.md)
- [런타임 동작](./how-this-works.md)
- [이론 배경](./theoretical_background.md)
- [운영 가이드](../ops/queueing-and-slo.md)
- [Python LLM 주입](../python/llm_injection.md)

## 1. 목표

Vtree Search는 멀티모달 문서 검색에서 아래 목표를 달성한다.

- 벡터 숏컷 기반 빠른 엔트리 탐색
- 트리(`ltree`) 기반 문맥 확장
- Python 주입형 LLM 필터/주석 적용
- 큐잉 기반 부하 제어와 tail latency 보호
- 적재 중간 상태(`summary_state`)의 명시적 기록/복구 기반 확보

## 2. 시스템 경계

### 라이브러리가 담당하는 것

- Rust 검색/적재 파이프라인(DB 조회/업서트)
- Python FFI 래퍼
- Python 공개 클래스
  - `VtreeIngestor`
  - `VTreeSearchEngine`
- Redis 검색 큐/잡 상태/DLQ
- Redis 적재 상태 저장소(`summary_state`)
- LangChain `ainvoke` 연동 어댑터

### 라이브러리가 담당하지 않는 것

- HTTP 서버 구현
- 인증/인가
- 멀티테넌시 정책
- API 라우팅

## 3. 계층 책임

### Python (`src_py/vtree_search`)

- `config/`: 설정 인터페이스
- `contracts/`: DTO/잡 상태 모델
- `runtime/`: Rust 브릿지 호출
- `queue/`: Redis Streams 관리
- `llm/`: 검색/적재 LLM 어댑터
- `ingestion/`: `VtreeIngestor` + 멀티모달 파서 + `summary_state` + `prompts/`
- `search/`: `VTreeSearchEngine`

### Rust (`src_rs`)

- `api/`: `SearchBridge`, `IngestionBridge`
- `core/`: 검색/적재 파이프라인
- `index/`: Postgres 저장소 + SQL 검증 유틸
- `math/`: 점수 보정 유틸

## 4. 검색 데이터 플로우

1. 앱이 `submit_search()` 호출
2. Python이 입력 검증 + fingerprint dedupe
3. Redis Stream에 job 적재 + `job:{id}.module_name` 기록(`REDIS_MODULE_SEARCH`)
4. 워커가 job을 읽고 Rust `SearchBridge.execute()` 호출
5. Rust가 `pgvector` 엔트리 조회 + `ltree` 하위 페이지 확장
6. Rust가 후보 dedupe + 조기 종료(`candidate_pool_factor`, `early_stop_min_entries`) 적용
7. Python이 확장 후보를 LangChain `ainvoke` 배치 필터로 판정
8. Python이 top-k 결과 저장 후 ACK

## 5. 적재 데이터 플로우

1. 앱이 `VtreeIngestor.upsert_document_from_path()` 호출
2. 문서 버전 단위 writer lock 획득(`sumstate:{document_id}:{version}:lock`)
3. SourceParser가 Markdown/PDF/DOCX 블록 추출
4. 표/이미지 블록을 LangChain `ainvoke`로 주석 생성
5. 헤딩 기반으로 sub-ch 경로를 생성하고 `sub-ch/ch/doc` 제목을 생성
6. Redis `summary_state`에 단계별 누적
   - `PAGE -> SUBCH -> CH -> DOC(DRAFT)`
7. Rust `IngestionBridge.execute()`로 summary/page 업서트
8. 성공 시 `DOC(FINAL)` + `meta=SUCCEEDED`, 실패 시 `meta=FAILED`

## 6. 큐/부하 제어 규약

- 큐 모델: Redis Streams + Consumer Group
- 보장 모델: At-least-once
- 임계치:
  - `QUEUE_MAX_LEN=200`
  - `QUEUE_REJECT_AT=180`
- 재시도: 3회 지수 백오프
- DLQ: `search:jobs:dlq`
- 포화 시 즉시 거절: `QueueOverloadedError`

## 7. 저장소 인터페이스

- DB: PostgreSQL + `pgvector` + `ltree`
- 입력 키:
  - `POSTGRES_DATABASE`, `VTREE_SUMMARY_TABLE`, `VTREE_PAGE_TABLE`
- 테이블 기본 인터페이스:
  - `summary_nodes(node_id, document_id, path, summary_text, embedding, metadata, updated_at)`
  - `page_nodes(node_id, parent_node_id, document_id, path, content, image_url, metadata, updated_at)`
- 중간 상태 저장소:
  - Redis `summary_state`
  - `meta`, `page_done`, `subch`, `ch`, `doc`

## 8. 실패 시맨틱

- 입력 오류: `ConfigurationError` / Rust `InvalidInput`
- 의존성 오류: `DependencyUnavailableError`
- 잡 실패: `JobFailedError`
- 큐 포화: `QueueOverloadedError`
- 결과 TTL 만료: `JobExpiredError`
- LLM 인터페이스 위반: 즉시 실패(재시도 정책 적용)
