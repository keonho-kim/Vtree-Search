"""
목적:
- 루트 `.env`를 읽어 VTreeSearchEngine을 실행하는 드라이버 스크립트를 제공한다.

설명:
- 라이브러리 본체는 환경 파일을 직접 읽지 않는다.
- 이 스크립트는 검색 잡 제출 -> 워커 1회 실행 -> 결과 조회 흐름을 데모한다.
- LLM 모델명은 `.env`에서 읽고 temperature는 코드 상수로 고정해 주입한다.

디자인 패턴:
- 드라이버(Driver Script).

참조:
- src_py/vtree_search/config/models.py
- src_py/vtree_search/search/engine.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI

from vtree_search import PostgresConfig, RedisQueueConfig, SearchConfig, VTreeSearchEngine

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
    "REDIS_STREAM_SEARCH",
    "REDIS_STREAM_SEARCH_DLQ",
    "REDIS_CONSUMER_GROUP",
    "QUEUE_MAX_LEN",
    "QUEUE_REJECT_AT",
    "JOB_RESULT_TTL_SEC",
    "WORKER_BLOCK_MS",
    "WORKER_CONCURRENCY",
    "JOB_MAX_RETRIES",
    "JOB_RETRY_BASE_MS",
    "JOB_RETRY_MAX_MS",
    "SEARCH_LLM_MODEL",
    "GOOGLE_API_KEY",
]
SEARCH_LLM_TEMPERATURE = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vtree Search 드라이버")
    parser.add_argument("--query", required=True, help="검색 질의 텍스트")
    parser.add_argument(
        "--embedding",
        required=True,
        help="콤마로 구분된 임베딩 벡터 (예: 0.1,0.2,0.3)",
    )
    parser.add_argument("--top-k", type=int, default=5, help="최종 후보 개수")
    parser.add_argument("--worker", default="run-search-worker", help="워커 이름")
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


def parse_embedding(raw: str, expected_dim: int) -> list[float]:
    values = [float(token.strip()) for token in raw.split(",") if token.strip()]
    if len(values) != expected_dim:
        raise RuntimeError(
            "--embedding 길이가 VTREE_EMBEDDING_DIM과 일치하지 않습니다: "
            f"expected={expected_dim}, actual={len(values)}"
        )
    return values


def build_config() -> SearchConfig:
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

    redis = RedisQueueConfig(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        db=int(os.environ["REDIS_DB"]),
        username=os.environ.get("REDIS_USERNAME") or None,
        password=os.environ.get("REDIS_PASSWORD") or None,
        use_ssl=parse_bool_env("REDIS_USE_SSL", "false"),
        module_name_search=os.environ.get("REDIS_MODULE_SEARCH", "VtreeSearch"),
        module_name_ingestion=os.environ.get("REDIS_MODULE_INGESTION", "VtreeIngestor"),
        stream_search=os.environ["REDIS_STREAM_SEARCH"],
        stream_search_dlq=os.environ["REDIS_STREAM_SEARCH_DLQ"],
        consumer_group=os.environ["REDIS_CONSUMER_GROUP"],
        queue_max_len=int(os.environ["QUEUE_MAX_LEN"]),
        queue_reject_at=int(os.environ["QUEUE_REJECT_AT"]),
        result_ttl_sec=int(os.environ["JOB_RESULT_TTL_SEC"]),
        worker_block_ms=int(os.environ["WORKER_BLOCK_MS"]),
    )

    return SearchConfig(
        postgres=postgres,
        redis=redis,
        worker_concurrency=int(os.environ["WORKER_CONCURRENCY"]),
        max_retries=int(os.environ["JOB_MAX_RETRIES"]),
        retry_base_ms=int(os.environ["JOB_RETRY_BASE_MS"]),
        retry_max_ms=int(os.environ["JOB_RETRY_MAX_MS"]),
        candidate_pool_factor=int(os.environ.get("SEARCH_CANDIDATE_POOL_FACTOR", "3")),
        early_stop_min_entries=int(os.environ.get("SEARCH_EARLY_STOP_MIN_ENTRIES", "1")),
    )


def parse_bool_env(key: str, default: str) -> bool:
    raw = os.environ.get(key, default).strip().lower()
    if raw in {"1", "true", "yes", "y"}:
        return True
    if raw in {"0", "false", "no", "n"}:
        return False
    raise RuntimeError(f"불리언 환경 변수 형식이 잘못되었습니다: {key}={raw}")


async def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    load_env_file(repo_root / args.env_file)
    ensure_required_env()

    config = build_config()
    embedding = parse_embedding(args.embedding, config.postgres.embedding_dim)

    llm = ChatGoogleGenerativeAI(
        model=os.environ["SEARCH_LLM_MODEL"],
        temperature=SEARCH_LLM_TEMPERATURE,
        google_api_key=os.environ["GOOGLE_API_KEY"],
    )

    engine = VTreeSearchEngine(config=config, llm=llm)

    accepted = engine.submit_search(
        query_text=args.query,
        query_embedding=embedding,
        top_k=args.top_k,
    )
    print("[submit]", accepted.model_dump_json(ensure_ascii=False))

    processed = await engine.run_worker_once(worker_name=args.worker, max_items=1)
    print(f"[worker] processed={processed}")

    status = engine.get_job(accepted.job_id)
    print("[status]", status.model_dump_json(ensure_ascii=False))

    if status.state == "SUCCEEDED":
        result = engine.fetch_result(accepted.job_id)
        print("[result]", result.model_dump_json(ensure_ascii=False))
    elif status.state == "FAILED":
        raise RuntimeError(status.last_error or "검색 작업이 실패했습니다")
    else:
        print("[notice] 아직 결과가 준비되지 않았습니다")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)
