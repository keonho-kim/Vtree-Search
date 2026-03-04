# Phase 1 전환 내역 (히스토리)

## 1. 목적

SVVE 기반 구조에서 Vtree Search 구조로 전환할 때의 초기 정비 이력을 기록한다.
이 문서는 **과거 전환 기록**이며 현재 구현 상태의 단일 기준은 아니다.

## 2. 적용된 변경

- 프로젝트 식별자 전환
  - `svve-core` -> `vtree-search`
  - `svve_core` -> `vtree_search`
  - `_svve_core` -> `_vtree_search`
- 소스 루트 분리
  - Python: `src_py/vtree_search`
  - Rust: `src_rs`
- 기존 SVVE 런타임 코드 제거
- 기존 문서 교체
- `new-design.md` 내용을 아키텍처 문서로 통합

## 3. 당시 이월 항목 (참고)

아래 항목은 Phase 1 시점에서 이월된 항목이며, 현재 저장소에서는 일부가 이미 구현되었다.

- PostgreSQL 실연동 쿼리
- Rust 검색/적재 실행 경로
- LangChain 기반 LLM 호출
- 운영 문서/설정 정합화

## 4. 현재 기준 문서

현재 동작 기준은 아래 문서를 따른다.

- [프로젝트 개요](../../README.md)
- [아키텍처 청사진](./blueprint.md)
- [런타임 동작](./how-this-works.md)
- [운영 가이드](../ops/queueing-and-slo.md)
