# Python 테스트 안내

## 목적

- `src_py/vtree_search` 공개 인터페이스의 스모크 검증 지점을 정의한다.

## 현재 포함 테스트

- `test_smoke_public_api.py`
  - 공개 API import 가능 여부
  - 주요 설정 모델 생성 검증
  - `PostgresConfig.to_dsn()` 포맷 검증

## 실행 핸드오프

- `uv run pytest tests/python`
