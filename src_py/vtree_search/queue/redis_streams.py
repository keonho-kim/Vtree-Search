"""
목적:
- Redis Streams 기반 검색 작업 큐를 관리한다.

설명:
- 작업 제출, 소비자 그룹 읽기, ACK, DLQ, 잡 상태 해시 저장을 담당한다.
- 라이브러리 계층에서 큐 포화 조건을 검사해 빠른 거절을 지원한다.

디자인 패턴:
- 저장소 패턴(Repository Pattern).

참조:
- src_py/vtree_search/search/engine.py
- src_py/vtree_search/contracts/job_models.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vtree_search.config.models import RedisQueueConfig
from vtree_search.exceptions import ConfigurationError, DependencyUnavailableError, QueueOverloadedError


@dataclass(slots=True)
class QueueMessage:
    """Redis Stream에서 꺼낸 단일 메시지 모델."""

    stream: str
    message_id: str
    fields: dict[str, str]


class RedisSearchQueue:
    """검색 작업용 Redis Streams 큐 매니저."""

    def __init__(self, config: RedisQueueConfig) -> None:
        self._config = config
        self._redis = self._create_client(config)

    @staticmethod
    def _create_client(config: RedisQueueConfig):
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

    @property
    def config(self) -> RedisQueueConfig:
        """큐 설정 객체를 반환한다."""
        return self._config

    def ensure_consumer_group(self) -> None:
        """소비자 그룹이 없으면 생성한다."""
        try:
            self._redis.xgroup_create(
                name=self._config.stream_search,
                groupname=self._config.consumer_group,
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            message = str(exc)
            if "BUSYGROUP" in message:
                return
            raise ConfigurationError(f"Redis consumer group 생성 실패: {exc}") from exc

    def queue_depth(self) -> int:
        """현재 검색 큐 길이를 반환한다."""
        try:
            depth = self._redis.xlen(self._config.stream_search)
        except Exception as exc:
            raise ConfigurationError(f"Redis xlen 조회 실패: {exc}") from exc
        return int(depth)

    def guard_capacity(self) -> None:
        """큐 포화 상태를 검사하고 초과 시 예외를 발생시킨다."""
        depth = self.queue_depth()
        if depth >= self._config.queue_reject_at:
            raise QueueOverloadedError(
                f"큐 포화 상태입니다: depth={depth}, reject_at={self._config.queue_reject_at}"
            )

    def create_job_record(self, job_id: str, payload_json: str, module_name: str) -> None:
        """잡 상태 해시를 초기화한다."""
        now = _utc_now()
        key = self._job_key(job_id)

        mapping = {
            "job_id": job_id,
            "state": "PENDING",
            "retries": "0",
            "canceled": "0",
            "created_at": now,
            "updated_at": now,
            "module_name": module_name,
            "payload_json": payload_json,
            "last_error": "",
            "result_json": "",
        }

        self._redis.hset(key, mapping=mapping)
        self._redis.expire(key, self._config.result_ttl_sec)

    def find_job_id_by_fingerprint(self, fingerprint: str) -> str | None:
        """fingerprint로 기존 잡 ID를 조회한다."""
        value = self._redis.get(self._fingerprint_key(fingerprint))
        if not value:
            return None
        return str(value)

    def claim_fingerprint(self, fingerprint: str, job_id: str) -> bool:
        """fingerprint 선점에 성공하면 True를 반환한다."""
        claimed = self._redis.set(
            self._fingerprint_key(fingerprint),
            job_id,
            nx=True,
            ex=self._config.result_ttl_sec,
        )
        return bool(claimed)

    def bind_fingerprint(self, fingerprint: str, job_id: str) -> None:
        """fingerprint를 지정 잡 ID에 바인딩한다."""
        self._redis.set(
            self._fingerprint_key(fingerprint),
            job_id,
            ex=self._config.result_ttl_sec,
        )

    def release_fingerprint_if_owner(self, fingerprint: str, job_id: str) -> None:
        """현재 owner일 때만 fingerprint 키를 제거한다."""
        key = self._fingerprint_key(fingerprint)
        current = self._redis.get(key)
        if str(current) != job_id:
            return
        self._redis.delete(key)

    def enqueue(
        self,
        job_id: str,
        payload_json: str,
        retries: int = 0,
        module_name: str = "",
    ) -> str:
        """검색 큐에 작업을 추가한다."""
        self._truncate_if_needed()

        fields = {
            "job_id": job_id,
            "payload_json": payload_json,
            "retries": str(retries),
            "module_name": module_name,
            "enqueued_at": _utc_now(),
        }

        message_id = self._redis.xadd(self._config.stream_search, fields=fields)
        return str(message_id)

    def read(self, consumer_name: str, count: int = 1) -> list[QueueMessage]:
        """소비자 그룹에서 작업을 읽는다."""
        response = self._redis.xreadgroup(
            groupname=self._config.consumer_group,
            consumername=consumer_name,
            streams={self._config.stream_search: ">"},
            count=count,
            block=self._config.worker_block_ms,
        )

        messages: list[QueueMessage] = []
        for stream, items in response:
            for message_id, fields in items:
                field_map = {str(key): str(value) for key, value in fields.items()}
                messages.append(
                    QueueMessage(
                        stream=str(stream),
                        message_id=str(message_id),
                        fields=field_map,
                    )
                )

        return messages

    def ack(self, message: QueueMessage) -> None:
        """처리 완료된 메시지를 ACK 한다."""
        self._redis.xack(message.stream, self._config.consumer_group, message.message_id)

    def move_to_dlq(self, message: QueueMessage, error_message: str) -> None:
        """실패 메시지를 DLQ로 이동한다."""
        fields = dict(message.fields)
        fields["moved_at"] = _utc_now()
        fields["error"] = error_message
        self._redis.xadd(self._config.stream_search_dlq, fields=fields)

    def get_job_record(self, job_id: str) -> dict[str, str] | None:
        """잡 상태 해시를 조회한다."""
        values = self._redis.hgetall(self._job_key(job_id))
        if not values:
            return None
        return {str(key): str(value) for key, value in values.items()}

    def update_job_record(self, job_id: str, mapping: dict[str, Any]) -> None:
        """잡 상태 해시를 부분 업데이트한다."""
        key = self._job_key(job_id)
        normalized = {str(key_): _normalize(value) for key_, value in mapping.items()}
        normalized["updated_at"] = _utc_now()
        self._redis.hset(key, mapping=normalized)
        self._redis.expire(key, self._config.result_ttl_sec)

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        """잡을 성공 상태로 마킹한다."""
        self.update_job_record(
            job_id,
            {
                "state": "SUCCEEDED",
                "result_json": json.dumps(result, ensure_ascii=False),
                "completed_at": _utc_now(),
                "last_error": "",
            },
        )

    def mark_failed(self, job_id: str, error_message: str, retries: int) -> None:
        """잡을 실패 상태로 마킹한다."""
        self.update_job_record(
            job_id,
            {
                "state": "FAILED",
                "retries": str(retries),
                "last_error": error_message,
                "completed_at": _utc_now(),
            },
        )

    def mark_running(self, job_id: str, retries: int) -> None:
        """잡을 실행 상태로 마킹한다."""
        self.update_job_record(
            job_id,
            {
                "state": "RUNNING",
                "retries": str(retries),
            },
        )

    def mark_pending_retry(self, job_id: str, retries: int, error_message: str) -> None:
        """재시도를 위해 잡을 대기 상태로 되돌린다."""
        self.update_job_record(
            job_id,
            {
                "state": "PENDING",
                "retries": str(retries),
                "last_error": error_message,
            },
        )

    def mark_canceled(self, job_id: str) -> None:
        """잡을 취소 상태로 마킹한다."""
        self.update_job_record(
            job_id,
            {
                "state": "CANCELED",
                "canceled": "1",
                "completed_at": _utc_now(),
            },
        )

    def mark_cancel_requested(self, job_id: str) -> None:
        """실행 전/중 잡에 취소 요청 플래그를 기록한다."""
        self.update_job_record(
            job_id,
            {
                "canceled": "1",
            },
        )

    def _truncate_if_needed(self) -> None:
        current = self.queue_depth()
        if current <= self._config.queue_max_len:
            return

        self._redis.xtrim(
            name=self._config.stream_search,
            maxlen=self._config.queue_max_len,
            approximate=True,
        )

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"job:{job_id}"

    @staticmethod
    def _fingerprint_key(fingerprint: str) -> str:
        return f"job:fp:{fingerprint}"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value)
