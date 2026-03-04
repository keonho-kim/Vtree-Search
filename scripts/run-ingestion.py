"""
목적:
- 루트 `.env`를 읽어 `VtreeIngestor`를 실행하는 드라이버 스크립트를 제공한다.

설명:
- 라이브러리 본체는 환경 파일을 직접 읽지 않는다.
- 이 스크립트는 파일 경로 기반 page 노드 생성 후 summary/page 업서트를 수행한다.
- 표/이미지 주석 모델명은 `.env`에서 읽고 temperature는 코드 상수로 고정해 주입한다.

디자인 패턴:
- 드라이버(Driver Script).

참조:
- src_py/vtree_search/config/models.py
- src_py/vtree_search/ingestion/ingestor.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI

from vtree_search import (
    IngestionConfig,
    IngestionPreprocessConfig,
    IngestionSummaryNode,
    PostgresConfig,
    SummaryStateConfig,
    VtreeIngestor,
)

REQUIRED_ENV_KEYS = [
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
    "POSTGRES_POOL_MIN",
    "POSTGRES_POOL_MAX",
    "POSTGRES_CONNECT_TIMEOUT_MS",
    "POSTGRES_STATEMENT_TIMEOUT_MS",
    "VTREE_SUMMARY_TABLE",
    "VTREE_PAGE_TABLE",
    "VTREE_EMBEDDING_DIM",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_DB",
    "INGESTION_LLM_MODEL",
    "GOOGLE_API_KEY",
]
INGESTION_LLM_TEMPERATURE = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vtree Ingestion 드라이버")
    parser.add_argument("--document-id", required=True, help="대상 문서 ID")
    parser.add_argument("--document-version", default="v1", help="문서 버전 식별자")
    parser.add_argument("--parent-node-id", required=True, help="page 노드의 부모(summary) 노드 ID")
    parser.add_argument("--summary-node-id", required=True, help="summary 노드 ID")
    parser.add_argument("--summary-path", required=True, help="summary 노드 ltree path")
    parser.add_argument("--summary-text", required=True, help="summary 노드 텍스트")
    parser.add_argument(
        "--summary-embedding",
        required=True,
        help="콤마로 구분된 summary 임베딩 벡터 (예: 0.1,0.2,0.3)",
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        required=True,
        help="ingestion 대상 파일 루트 경로",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="확장자별 1개 파일만 처리",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="루트 기준 환경 파일 경로 (기본: .env)",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"환경 파일이 존재하지 않습니다: {path}")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def ensure_required_env() -> None:
    missing = [key for key in REQUIRED_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"필수 환경 변수가 누락되었습니다: {', '.join(missing)}")


def parse_bool_env(key: str, default: str) -> bool:
    raw = os.environ.get(key, default).strip().lower()
    if raw in {"1", "true", "yes", "y"}:
        return True
    if raw in {"0", "false", "no", "n"}:
        return False
    raise RuntimeError(f"불리언 환경 변수 형식이 잘못되었습니다: {key}={raw}")


def parse_embedding(raw: str, expected_dim: int) -> list[float]:
    values = [float(token.strip()) for token in raw.split(",") if token.strip()]
    if len(values) != expected_dim:
        raise RuntimeError(
            "--summary-embedding 길이가 VTREE_EMBEDDING_DIM과 일치하지 않습니다: "
            f"expected={expected_dim}, actual={len(values)}"
        )
    return values


def build_config() -> IngestionConfig:
    postgres = PostgresConfig(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DATABASE"],
        summary_table=os.environ["VTREE_SUMMARY_TABLE"],
        page_table=os.environ["VTREE_PAGE_TABLE"],
        embedding_dim=int(os.environ["VTREE_EMBEDDING_DIM"]),
        pool_min=int(os.environ["POSTGRES_POOL_MIN"]),
        pool_max=int(os.environ["POSTGRES_POOL_MAX"]),
        connect_timeout_ms=int(os.environ["POSTGRES_CONNECT_TIMEOUT_MS"]),
        statement_timeout_ms=int(os.environ["POSTGRES_STATEMENT_TIMEOUT_MS"]),
    )

    preprocess = IngestionPreprocessConfig(
        max_chunk_chars=int(os.environ.get("INGEST_MAX_CHUNK_CHARS", "1024")),
        sample_per_extension=parse_bool_env("INGEST_SAMPLE_PER_EXTENSION", "false"),
        enable_table_annotation=parse_bool_env("INGEST_ENABLE_TABLE_ANNOTATION", "true"),
        enable_image_annotation=parse_bool_env("INGEST_ENABLE_IMAGE_ANNOTATION", "true"),
        asset_output_dir=os.environ.get("INGEST_ASSET_OUTPUT_DIR", "data/ingestion-assets"),
    )

    summary_state = SummaryStateConfig(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        db=int(os.environ["REDIS_DB"]),
        username=os.environ.get("REDIS_USERNAME") or None,
        password=os.environ.get("REDIS_PASSWORD") or None,
        use_ssl=parse_bool_env("REDIS_USE_SSL", "false"),
        ttl_sec=int(os.environ.get("SUMMARY_STATE_TTL_SEC", "86400")),
        lock_ttl_sec=int(os.environ.get("SUMMARY_STATE_LOCK_TTL_SEC", "120")),
    )

    return IngestionConfig(postgres=postgres, preprocess=preprocess, summary_state=summary_state)


async def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    load_env_file(repo_root / args.env_file)
    ensure_required_env()

    config = build_config()
    summary_embedding = parse_embedding(
        raw=args.summary_embedding,
        expected_dim=config.postgres.embedding_dim,
    )

    annotation_enabled = (
        config.preprocess.enable_table_annotation or config.preprocess.enable_image_annotation
    )
    llm = None
    if annotation_enabled:
        llm = ChatGoogleGenerativeAI(
            model=os.environ["INGESTION_LLM_MODEL"],
            temperature=INGESTION_LLM_TEMPERATURE,
            google_api_key=os.environ["GOOGLE_API_KEY"],
        )

    summary_node = IngestionSummaryNode(
        node_id=args.summary_node_id,
        document_id=args.document_id,
        path=args.summary_path,
        summary_text=args.summary_text,
        embedding=summary_embedding,
        metadata={"source": "scripts/run-ingestion.py"},
    )

    ingestor = VtreeIngestor(config=config, llm=llm)
    result = await ingestor.upsert_document_from_path(
        document_id=args.document_id,
        summary_nodes=[summary_node],
        parent_node_id=args.parent_node_id,
        input_root=args.input_root,
        sample=args.sample,
        document_version=args.document_version,
    )

    print("[ingestion-result]", result.model_dump_json(ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)
