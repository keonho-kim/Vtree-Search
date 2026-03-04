"""
목적:
- 공개 Python 인터페이스의 스모크 동작을 검증한다.

설명:
- 외부 Redis/Postgres 연결 없이 import/모델 생성/DSN 포맷을 확인한다.

디자인 패턴:
- 스모크 테스트(Smoke Test).

참조:
- src_py/vtree_search/__init__.py
- src_py/vtree_search/config/models.py
"""

from __future__ import annotations

from vtree_search import IngestionConfig, IngestionPreprocessConfig, PostgresConfig, SummaryStateConfig


def test_공개_설정_모델을_생성할_수_있다() -> None:
    postgres = PostgresConfig(
        host="localhost",
        port=5432,
        user="postgres",
        password="secret",
        database="vtree",
        summary_table="summary_nodes",
        page_table="page_nodes",
        embedding_dim=4,
        pool_min=1,
        pool_max=4,
        connect_timeout_ms=2000,
        statement_timeout_ms=3000,
    )
    summary_state = SummaryStateConfig(
        host="localhost",
        port=6379,
        db=0,
        ttl_sec=86400,
        lock_ttl_sec=120,
    )

    config = IngestionConfig(
        postgres=postgres,
        preprocess=IngestionPreprocessConfig(),
        summary_state=summary_state,
    )

    assert config.postgres.database == "vtree"
    assert config.summary_state is not None
    assert config.summary_state.ttl_sec == 86400


def test_postgres_dsn_포맷이_예상과_일치한다() -> None:
    config = PostgresConfig(
        host="db.internal",
        port=5433,
        user="user name",
        password="p@ss word",
        database="service db",
        summary_table="summary_nodes",
        page_table="page_nodes",
        embedding_dim=8,
    )

    dsn = config.to_dsn()
    assert dsn == "postgresql://user%20name:p%40ss%20word@db.internal:5433/service%20db"
