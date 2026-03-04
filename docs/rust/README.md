# Rust 계층 개요

## 관련 문서

- [프로젝트 개요](../../README.md)
- [아키텍처 청사진](../arch/blueprint.md)
- [런타임 동작](../arch/how-this-works.md)
- [Rust 모듈 레퍼런스](./module_reference.md)
- [Python 개요](../python/README.md)

## 목적

`src_rs`는 검색/적재 핵심 실행 경로를 담당한다.

## 모듈 책임

- `api/`
  - `SearchBridge`: 검색 JSON 실행 진입점
  - `IngestionBridge`: 적재 JSON 실행 진입점
- `core/`
  - `search_pipeline`: DB 검색/후보 확장(후보 dedupe + 조기 종료)
  - `ingestion_pipeline`: 적재 파이프라인(upsert/rebuild)
  - `errors`: 오류 모델
- `index/`
  - `postgres_repo`: 조회/upsert 저장소
  - `sql`: 식별자/벡터 리터럴 검증 유틸
- `math/`
  - 점수 보정 유틸

## 운영 원칙

- DB 쿼리는 Rust에서 직접 수행
- LLM 필터/주석은 Python 계층에서 수행
- 오류는 문자열이 아닌 구조적 타입(`CoreError`)으로 분류
- Python 계층과는 JSON payload 인터페이스로 통신
