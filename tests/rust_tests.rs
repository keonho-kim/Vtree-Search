// 목적:
// - Rust SQL 유틸의 스모크 동작을 검증한다.
//
// 설명:
// - DB 연결 없이 식별자 검증/벡터 리터럴 변환의 성공/실패 경로를 확인한다.
//
// 디자인 패턴:
// - 스모크 테스트(Smoke Test).
//
// 참조:
// - src_rs/index/sql.rs

use _vtree_search::index::sql::{to_pgvector_literal, validate_identifier};

#[test]
fn validate_identifier_허용_문자를_통과한다() {
    let result = validate_identifier("summary_nodes_2026", "summary_table");
    assert!(result.is_ok());
}

#[test]
fn validate_identifier_허용되지_않은_문자를_거부한다() {
    let result = validate_identifier("summary-nodes", "summary_table");
    assert!(result.is_err());
}

#[test]
fn to_pgvector_literal_정상_포맷을_반환한다() {
    let literal = to_pgvector_literal(&[0.1_f32, -2.5_f32, 3.0_f32]).expect("벡터 변환 실패");
    assert_eq!(literal, "[0.10000000,-2.50000000,3.00000000]");
}

#[test]
fn to_pgvector_literal_빈_벡터를_거부한다() {
    let result = to_pgvector_literal(&[]);
    assert!(result.is_err());
}
