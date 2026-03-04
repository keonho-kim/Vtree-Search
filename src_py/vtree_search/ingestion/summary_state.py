"""
목적:
- Redis 기반 문서 요약 누적 상태 저장소를 제공한다.

설명:
- 문서 단위 writer 락, run 메타 상태, page 완료 집합, ch/doc 요약 상태를 관리한다.
- 적재 워커 재시도/복구를 위해 상태를 TTL 기반으로 보관한다.

디자인 패턴:
- 저장소 패턴(Repository Pattern).

참조:
- src_py/vtree_search/config/models.py
- src_py/vtree_search/ingestion/ingestor.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from vtree_search.config.models import SummaryStateConfig
from vtree_search.exceptions import ConfigurationError, DependencyUnavailableError


class RedisSummaryStateStore:
    """Redis 기반 summary_state 저장소."""

    def __init__(self, config: SummaryStateConfig) -> None:
        self._config = config
        self._redis = self._create_client(config)

    @staticmethod
    def _create_client(config: SummaryStateConfig):
        try:
            import redis
        except Exception as exc:  # pragma: no cover - 런타임 환경 의존
            raise DependencyUnavailableError(f"redis 패키지를 불러오지 못했습니다: {exc}") from exc

        return redis.Redis(
            host=config.host,
            port=config.port,
            db=config.db,
            username=config.username,
            password=config.password,
            ssl=config.use_ssl,
            decode_responses=True,
        )

    def acquire_writer_lock(self, *, document_id: str, version: str, lock_token: str) -> bool:
        """문서 버전 단위 writer 락을 획득한다."""
        lock_key = self._lock_key(document_id=document_id, version=version)
        acquired = self._redis.set(
            lock_key,
            lock_token,
            nx=True,
            ex=self._config.lock_ttl_sec,
        )
        return bool(acquired)

    def refresh_writer_lock(self, *, document_id: str, version: str, lock_token: str) -> bool:
        """현재 락 소유자일 때만 락 TTL을 갱신한다."""
        lock_key = self._lock_key(document_id=document_id, version=version)
        script = """
        local key = KEYS[1]
        local token = ARGV[1]
        local ttl = tonumber(ARGV[2])
        if redis.call("GET", key) == token then
          return redis.call("EXPIRE", key, ttl)
        end
        return 0
        """
        updated = self._redis.eval(script, 1, lock_key, lock_token, str(self._config.lock_ttl_sec))
        return int(updated) == 1

    def release_writer_lock(self, *, document_id: str, version: str, lock_token: str) -> bool:
        """현재 락 소유자일 때만 락을 해제한다."""
        lock_key = self._lock_key(document_id=document_id, version=version)
        script = """
        local key = KEYS[1]
        local token = ARGV[1]
        if redis.call("GET", key) == token then
          return redis.call("DEL", key)
        end
        return 0
        """
        deleted = self._redis.eval(script, 1, lock_key, lock_token)
        return int(deleted) == 1

    def initialize_run(
        self,
        *,
        document_id: str,
        version: str,
        run_id: str,
        lock_token: str,
        total_pages: int,
        total_subch: int,
        total_ch: int,
    ) -> None:
        """summary_state run 초기 상태를 기록한다."""
        now = _utc_now()
        meta_key = self._meta_key(document_id=document_id, version=version, run_id=run_id)
        mapping = {
            "document_id": document_id,
            "version": version,
            "run_id": run_id,
            "status": "RUNNING",
            "stage": "PAGE",
            "attempt": "0",
            "started_at": now,
            "updated_at": now,
            "done_pages": "0",
            "done_subch": "0",
            "done_ch": "0",
            "total_pages": str(max(0, total_pages)),
            "total_subch": str(max(0, total_subch)),
            "total_ch": str(max(0, total_ch)),
            "last_error": "",
            "lock_token": lock_token,
        }
        self._redis.hset(meta_key, mapping=mapping)
        self._touch_run_ttl(document_id=document_id, version=version, run_id=run_id)

    def update_meta(
        self,
        *,
        document_id: str,
        version: str,
        run_id: str,
        stage: str,
        status: str,
        attempt: int | None = None,
        last_error: str = "",
        done_pages: int | None = None,
        done_subch: int | None = None,
        done_ch: int | None = None,
    ) -> None:
        """run 메타 상태를 업데이트한다."""
        meta_key = self._meta_key(document_id=document_id, version=version, run_id=run_id)
        mapping: dict[str, str] = {
            "stage": stage,
            "status": status,
            "updated_at": _utc_now(),
            "last_error": last_error,
        }
        if attempt is not None:
            mapping["attempt"] = str(max(0, attempt))
        if done_pages is not None:
            mapping["done_pages"] = str(max(0, done_pages))
        if done_subch is not None:
            mapping["done_subch"] = str(max(0, done_subch))
        if done_ch is not None:
            mapping["done_ch"] = str(max(0, done_ch))

        self._redis.hset(meta_key, mapping=mapping)
        self._touch_run_ttl(document_id=document_id, version=version, run_id=run_id)

    def add_page_done(
        self,
        *,
        document_id: str,
        version: str,
        run_id: str,
        page_paths: list[str],
    ) -> int:
        """완료 page 경로를 집합에 추가하고 누적 개수를 반환한다."""
        key = self._page_done_key(document_id=document_id, version=version, run_id=run_id)
        if page_paths:
            self._redis.sadd(key, *page_paths)
        count = int(self._redis.scard(key))
        self.update_meta(
            document_id=document_id,
            version=version,
            run_id=run_id,
            stage="PAGE",
            status="RUNNING",
            done_pages=count,
        )
        return count

    def upsert_subch_state(
        self,
        *,
        document_id: str,
        version: str,
        run_id: str,
        tree_path: str,
        title: str,
        summary_text: str,
        qa_topics: list[str],
        evidence_pages: list[str],
        source_page_count: int,
        revision: int,
        status: str = "READY",
    ) -> bool:
        """sub-ch 상태를 revision 증가 조건으로 갱신한다."""
        key = self._subch_key(document_id=document_id, version=version, run_id=run_id)
        payload = {
            "status": status,
            "title": title,
            "summary_text": summary_text,
            "qa_topics": qa_topics,
            "evidence_pages": evidence_pages,
            "source_page_count": max(0, source_page_count),
            "revision": max(1, revision),
            "updated_at": _utc_now(),
        }
        changed = self._hset_json_if_newer(key=key, field=tree_path, payload=payload)
        if changed:
            self._touch_run_ttl(document_id=document_id, version=version, run_id=run_id)
        return changed

    def upsert_ch_state(
        self,
        *,
        document_id: str,
        version: str,
        run_id: str,
        tree_path: str,
        title: str,
        summary_text: str,
        qa_topics: list[str],
        covered_subch: list[str],
        revision: int,
        status: str = "READY",
    ) -> bool:
        """ch 상태를 revision 증가 조건으로 갱신한다."""
        key = self._ch_key(document_id=document_id, version=version, run_id=run_id)
        payload = {
            "status": status,
            "title": title,
            "summary_text": summary_text,
            "qa_topics": qa_topics,
            "covered_subch": covered_subch,
            "revision": max(1, revision),
            "updated_at": _utc_now(),
        }
        changed = self._hset_json_if_newer(key=key, field=tree_path, payload=payload)
        if changed:
            self._touch_run_ttl(document_id=document_id, version=version, run_id=run_id)
        return changed

    def upsert_doc_state(
        self,
        *,
        document_id: str,
        version: str,
        run_id: str,
        title: str,
        summary_text: str,
        qa_topics: list[str],
        coverage_map: dict[str, list[str]],
        revision: int,
        status: str,
    ) -> bool:
        """doc 상태를 revision 증가 조건으로 갱신한다."""
        key = self._doc_key(document_id=document_id, version=version, run_id=run_id)
        current_revision = int(self._redis.hget(key, "revision") or "0")
        next_revision = max(1, revision)
        if current_revision >= next_revision:
            return False

        mapping = {
            "status": status,
            "title": title,
            "summary_text": summary_text,
            "qa_topics": json.dumps(qa_topics, ensure_ascii=False),
            "coverage_map": json.dumps(coverage_map, ensure_ascii=False),
            "revision": str(next_revision),
            "updated_at": _utc_now(),
        }
        self._redis.hset(key, mapping=mapping)
        self._touch_run_ttl(document_id=document_id, version=version, run_id=run_id)
        return True

    def mark_failed(
        self,
        *,
        document_id: str,
        version: str,
        run_id: str,
        error_message: str,
    ) -> None:
        """run 상태를 실패로 마킹한다."""
        self.update_meta(
            document_id=document_id,
            version=version,
            run_id=run_id,
            stage="COMMIT",
            status="FAILED",
            last_error=error_message,
        )

    def mark_succeeded(self, *, document_id: str, version: str, run_id: str) -> None:
        """run 상태를 성공으로 마킹한다."""
        self.update_meta(
            document_id=document_id,
            version=version,
            run_id=run_id,
            stage="COMMIT",
            status="SUCCEEDED",
            last_error="",
        )

    def _touch_run_ttl(self, *, document_id: str, version: str, run_id: str) -> None:
        keys = [
            self._meta_key(document_id=document_id, version=version, run_id=run_id),
            self._page_done_key(document_id=document_id, version=version, run_id=run_id),
            self._subch_key(document_id=document_id, version=version, run_id=run_id),
            self._ch_key(document_id=document_id, version=version, run_id=run_id),
            self._doc_key(document_id=document_id, version=version, run_id=run_id),
        ]
        for key in keys:
            self._redis.expire(key, self._config.ttl_sec)

    def _hset_json_if_newer(self, *, key: str, field: str, payload: dict[str, Any]) -> bool:
        current_raw = self._redis.hget(key, field)
        current_revision = 0
        if current_raw:
            try:
                current = json.loads(current_raw)
                current_revision = int(current.get("revision", 0))
            except Exception as exc:
                raise ConfigurationError(f"summary_state JSON 파싱 실패: key={key}, field={field}, {exc}") from exc

        next_revision = int(payload.get("revision", 0))
        if current_revision >= next_revision:
            return False

        self._redis.hset(key, field, json.dumps(payload, ensure_ascii=False))
        return True

    @staticmethod
    def _lock_key(*, document_id: str, version: str) -> str:
        return f"sumstate:{document_id}:{version}:lock"

    @staticmethod
    def _meta_key(*, document_id: str, version: str, run_id: str) -> str:
        return f"sumstate:{document_id}:{version}:{run_id}:meta"

    @staticmethod
    def _page_done_key(*, document_id: str, version: str, run_id: str) -> str:
        return f"sumstate:{document_id}:{version}:{run_id}:page_done"

    @staticmethod
    def _subch_key(*, document_id: str, version: str, run_id: str) -> str:
        return f"sumstate:{document_id}:{version}:{run_id}:subch"

    @staticmethod
    def _ch_key(*, document_id: str, version: str, run_id: str) -> str:
        return f"sumstate:{document_id}:{version}:{run_id}:ch"

    @staticmethod
    def _doc_key(*, document_id: str, version: str, run_id: str) -> str:
        return f"sumstate:{document_id}:{version}:{run_id}:doc"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()
