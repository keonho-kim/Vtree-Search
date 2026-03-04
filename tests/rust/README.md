# Rust 테스트 안내

## 목적

- `src_rs` 유틸/코어 경계의 스모크 검증 지점을 정의한다.

## 현재 포함 테스트

- `tests/rust_tests.rs`
  - `validate_identifier()` 허용/거부 케이스
  - `to_pgvector_literal()` 포맷/빈 입력 오류 케이스

## 실행 핸드오프

- `cargo test --test rust_tests`
