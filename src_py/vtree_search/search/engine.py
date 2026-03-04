"""
목적:
- Redis 큐잉 기반 검색 엔진 클래스를 제공한다.

설명:
- 검색 요청을 큐에 적재하고 잡 상태/결과를 조회한다.
- 워커에서 Rust 검색 브릿지를 호출한 뒤, LLM 배치 필터를 적용해 최종 결과를 생성한다.

디자인 패턴:
- 서비스 레이어(Service Layer) + 큐 소비자(Worker).

참조:
- src_py/vtree_search/runtime/bridge.py
- src_py/vtree_search/queue/redis_streams.py
- src_py/vtree_search/contracts/job_models.py
- src_py/vtree_search/llm/langchain_search.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from vtree_search.config.models import SearchConfig
from vtree_search.contracts.job_models import (
    JobState,
    SearchCandidate,
    SearchJobAccepted,
    SearchJobCanceled,
    SearchJobResult,
    SearchJobStatus,
)
from vtree_search.contracts.search_models import SearchSubmission
from vtree_search.exceptions import (
    ConfigurationError,
    JobExpiredError,
    JobFailedError,
    JobNotFoundError,
)
from vtree_search.llm.contracts import SearchFilterCandidate, SearchFilterDecision
from vtree_search.llm.langchain_search import LangChainSearchFilterLLM
from vtree_search.queue.redis_streams import QueueMessage, RedisSearchQueue
from vtree_search.runtime.bridge import RustRuntimeBridge


class VTreeSearchEngine:
    """Redis 큐를 사용하는 검색 엔진 클래스."""

    def __init__(
        self,
        config: SearchConfig,
        llm,
        runtime_bridge: RustRuntimeBridge | None = None,
        queue: RedisSearchQueue | None = None,
    ) -> None:
        self._config = config
        self._filter_llm = LangChainSearchFilterLLM(chat_model=llm)
        self._runtime_bridge = runtime_bridge or RustRuntimeBridge()
        self._queue = queue or RedisSearchQueue(config.redis)
        self._queue.ensure_consumer_group()

    def submit_search(
        self,
        query_text: str,
        query_embedding: list[float],
        top_k: int = 5,
        metadata: dict[str, object] | None = None,
    ) -> SearchJobAccepted:
        """검색 작업을 큐에 제출한다."""
        if top_k < 1:
            raise ConfigurationError("top_k는 1 이상이어야 합니다")

        if len(query_embedding) != self._config.postgres.embedding_dim:
            raise ConfigurationError(
                "query_embedding 길이가 embedding_dim과 일치하지 않습니다: "
                f"expected={self._config.postgres.embedding_dim}, actual={len(query_embedding)}"
            )

        fingerprint = _build_search_fingerprint(
            query_text=query_text,
            query_embedding=query_embedding,
            top_k=top_k,
            metadata=metadata,
        )
        reusable = self._find_reusable_job(fingerprint)
        if reusable is not None:
            return reusable

        job_id = uuid4().hex
        self._queue.guard_capacity()

        if not self._queue.claim_fingerprint(fingerprint, job_id):
            reusable = self._find_reusable_job(fingerprint)
            if reusable is not None:
                return reusable
            self._queue.bind_fingerprint(fingerprint, job_id)

        submission = SearchSubmission(
            job_id=job_id,
            query_text=query_text,
            query_embedding=query_embedding,
            top_k=top_k,
            metadata=metadata,
        )

        payload = self._build_rust_payload(submission)
        payload_json = json.dumps(payload, ensure_ascii=False)
        module_name = self._config.redis.module_name_search

        try:
            self._queue.create_job_record(job_id, payload_json, module_name=module_name)
            self._queue.enqueue(
                job_id=job_id,
                payload_json=payload_json,
                retries=0,
                module_name=module_name,
            )
        except Exception:
            self._queue.release_fingerprint_if_owner(fingerprint, job_id)
            raise

        return SearchJobAccepted(
            job_id=job_id,
            state="PENDING",
            submitted_at=_utc_now(),
        )

    def _find_reusable_job(self, fingerprint: str) -> SearchJobAccepted | None:
        existing_job_id = self._queue.find_job_id_by_fingerprint(fingerprint)
        if existing_job_id is None:
            return None

        record = self._queue.get_job_record(existing_job_id)
        if record is None:
            return None

        state_raw = str(record.get("state", "PENDING"))
        if state_raw not in {"PENDING", "RUNNING", "SUCCEEDED"}:
            return None

        state = cast(JobState, state_raw)
        submitted_at = str(record.get("created_at") or record.get("updated_at") or _utc_now())
        return SearchJobAccepted(
            job_id=existing_job_id,
            state=state,
            submitted_at=submitted_at,
        )

    def get_job(self, job_id: str) -> SearchJobStatus:
        """잡 상태를 조회한다."""
        record = self._queue.get_job_record(job_id)
        if record is None:
            raise JobNotFoundError(f"job_id={job_id}를 찾을 수 없습니다")

        return SearchJobStatus.model_validate(
            {
                "job_id": job_id,
                "state": record.get("state", "PENDING"),
                "retries": int(record.get("retries", "0") or "0"),
                "canceled": record.get("canceled", "0") == "1",
                "updated_at": str(record.get("updated_at", _utc_now())),
                "last_error": record.get("last_error") or None,
            }
        )

    def fetch_result(self, job_id: str) -> SearchJobResult:
        """성공한 잡의 결과를 조회한다."""
        record = self._queue.get_job_record(job_id)
        if record is None:
            raise JobExpiredError(f"job_id={job_id} 결과가 만료되었거나 존재하지 않습니다")

        state = str(record.get("state", "PENDING"))
        if state == "FAILED":
            raise JobFailedError(record.get("last_error") or f"job_id={job_id}가 실패했습니다")
        if state != "SUCCEEDED":
            raise JobFailedError(f"job_id={job_id} 상태가 SUCCEEDED가 아닙니다: {state}")

        result_json = record.get("result_json") or ""
        if not result_json:
            raise JobFailedError(f"job_id={job_id}의 result_json이 비어 있습니다")

        try:
            payload = json.loads(result_json)
        except json.JSONDecodeError as exc:
            raise JobFailedError(f"job_id={job_id} 결과 JSON 파싱 실패: {exc}") from exc

        payload["state"] = "SUCCEEDED"
        payload["completed_at"] = record.get("completed_at") or record.get("updated_at") or _utc_now()
        return SearchJobResult.model_validate(payload)

    def cancel_job(self, job_id: str) -> SearchJobCanceled:
        """잡 취소를 요청한다."""
        record = self._queue.get_job_record(job_id)
        if record is None:
            raise JobNotFoundError(f"job_id={job_id}를 찾을 수 없습니다")

        state = str(record.get("state", "PENDING"))
        if state in {"SUCCEEDED", "FAILED", "CANCELED"}:
            return SearchJobCanceled(
                job_id=job_id,
                state="CANCELED",
                message=f"이미 종결된 작업입니다: current_state={state}",
            )

        self._queue.mark_cancel_requested(job_id)
        if state == "PENDING":
            self._queue.mark_canceled(job_id)

        return SearchJobCanceled(
            job_id=job_id,
            state="CANCELED",
            message="취소 요청이 접수되었습니다",
        )

    async def run_worker_once(self, worker_name: str, max_items: int = 1) -> int:
        """큐에서 최대 max_items개 작업을 처리한다."""
        if max_items < 1:
            raise ConfigurationError("max_items는 1 이상이어야 합니다")

        messages = self._queue.read(consumer_name=worker_name, count=max_items)
        processed = 0

        for message in messages:
            await self._process_message(message)
            processed += 1

        return processed

    async def run_worker_forever(self, worker_name: str) -> None:
        """큐 작업을 지속적으로 처리한다."""
        while True:
            processed = await self.run_worker_once(worker_name=worker_name, max_items=1)
            if processed == 0:
                await asyncio.sleep(0.05)

    async def _process_message(self, message: QueueMessage) -> None:
        job_id = message.fields.get("job_id", "")
        if not job_id:
            self._queue.ack(message)
            return

        record = self._queue.get_job_record(job_id)
        if record is None:
            self._queue.ack(message)
            return

        state = str(record.get("state", "PENDING"))
        if state in {"SUCCEEDED", "FAILED", "CANCELED"}:
            self._queue.ack(message)
            return

        if record.get("canceled", "0") == "1":
            self._queue.mark_canceled(job_id)
            self._queue.ack(message)
            return

        record_retries = int(record.get("retries", "0") or "0")
        retries = int(message.fields.get("retries", str(record_retries)) or "0")
        if retries < record_retries:
            self._queue.ack(message)
            return
        self._queue.mark_running(job_id, retries)

        payload_json = message.fields.get("payload_json") or record.get("payload_json") or ""
        if not payload_json:
            self._queue.mark_failed(job_id, "payload_json이 비어 있습니다", retries)
            self._queue.move_to_dlq(message, "payload_json-empty")
            self._queue.ack(message)
            return

        try:
            payload = json.loads(payload_json)
            rust_result = await asyncio.to_thread(self._runtime_bridge.execute_search_job, payload)
            result = await self._build_filtered_result(payload=payload, rust_result=rust_result)
        except Exception as exc:  # noqa: BLE001
            next_retry = retries + 1
            error_message = str(exc)

            if next_retry <= self._config.max_retries:
                backoff_ms = min(
                    self._config.retry_base_ms * (2 ** max(0, next_retry - 1)),
                    self._config.retry_max_ms,
                )
                self._queue.mark_pending_retry(job_id, next_retry, error_message)
                self._queue.enqueue(
                    job_id=job_id,
                    payload_json=payload_json,
                    retries=next_retry,
                    module_name=record.get("module_name", self._config.redis.module_name_search),
                )
                await asyncio.sleep(backoff_ms / 1000.0)
            else:
                self._queue.mark_failed(job_id, error_message, next_retry)
                self._queue.move_to_dlq(message, error_message)

            self._queue.ack(message)
            return

        self._queue.mark_succeeded(job_id, result)
        self._queue.ack(message)

    async def _build_filtered_result(
        self,
        *,
        payload: dict[str, Any],
        rust_result: dict[str, Any],
    ) -> dict[str, Any]:
        question = str(payload["question"])
        top_k = int(payload["top_k"])

        raw_candidates = rust_result.get("candidates")
        if not isinstance(raw_candidates, list):
            raise JobFailedError("Rust 검색 결과 candidates가 배열이 아닙니다")

        candidates = [SearchCandidate.model_validate(item) for item in raw_candidates]
        if not candidates:
            raw_metrics = rust_result.get("metrics")
            if not isinstance(raw_metrics, dict):
                raise JobFailedError("Rust 검색 결과 metrics가 객체가 아닙니다")
            return {
                "job_id": str(payload["job_id"]),
                "candidates": [],
                "metrics": {
                    "entry_count": int(raw_metrics["entry_count"]),
                    "page_count": int(raw_metrics["page_count"]),
                    "kept_count": 0,
                    "elapsed_ms": int(raw_metrics["elapsed_ms"]),
                },
            }

        filter_candidates = [
            SearchFilterCandidate(node_id=candidate.node_id, content=candidate.content)
            for candidate in candidates
        ]

        decisions = await self._filter_llm.filter(question=question, candidates=filter_candidates)
        decision_map = _to_decision_map(candidates=filter_candidates, decisions=decisions)

        kept: list[SearchCandidate] = []
        for candidate in candidates:
            decision = decision_map[candidate.node_id]
            if not decision.keep:
                continue
            kept.append(
                SearchCandidate(
                    node_id=candidate.node_id,
                    path=candidate.path,
                    score=candidate.score,
                    content=candidate.content,
                    image_url=candidate.image_url,
                    reason=decision.reason,
                )
            )

        kept.sort(
            key=lambda item: (-item.score, item.path),
        )
        if len(kept) > top_k:
            kept = kept[:top_k]

        raw_metrics = rust_result.get("metrics")
        if not isinstance(raw_metrics, dict):
            raise JobFailedError("Rust 검색 결과 metrics가 객체가 아닙니다")

        entry_count = int(raw_metrics["entry_count"])
        page_count = int(raw_metrics["page_count"])
        elapsed_ms = int(raw_metrics["elapsed_ms"])

        return {
            "job_id": str(payload["job_id"]),
            "candidates": [candidate.model_dump() for candidate in kept],
            "metrics": {
                "entry_count": entry_count,
                "page_count": page_count,
                "kept_count": len(kept),
                "elapsed_ms": elapsed_ms,
            },
        }

    def _build_rust_payload(self, submission: SearchSubmission) -> dict[str, Any]:
        return {
            "job_id": submission.job_id,
            "question": submission.query_text,
            "query_embedding": submission.query_embedding,
            "top_k": submission.top_k,
            "entry_limit": self._config.entry_limit,
            "page_limit": self._config.page_limit,
            "candidate_pool_factor": self._config.candidate_pool_factor,
            "early_stop_min_entries": self._config.early_stop_min_entries,
            "worker_concurrency": self._config.worker_concurrency,
            "postgres": {
                "dsn": self._config.postgres.to_dsn(),
                "summary_table": self._config.postgres.summary_table,
                "page_table": self._config.postgres.page_table,
                "pool_min": self._config.postgres.pool_min,
                "pool_max": self._config.postgres.pool_max,
                "connect_timeout_ms": self._config.postgres.connect_timeout_ms,
                "statement_timeout_ms": self._config.postgres.statement_timeout_ms,
            },
            "metadata": submission.metadata,
        }


def _to_decision_map(
    *,
    candidates: list[SearchFilterCandidate],
    decisions: list[SearchFilterDecision],
) -> dict[str, SearchFilterDecision]:
    expected_ids = {candidate.node_id for candidate in candidates}
    mapped: dict[str, SearchFilterDecision] = {}

    for decision in decisions:
        node_id = decision.node_id
        if node_id not in expected_ids:
            raise JobFailedError(f"검색 필터 응답 node_id가 후보 집합에 없습니다: {node_id}")
        if node_id in mapped:
            raise JobFailedError(f"검색 필터 응답 node_id가 중복되었습니다: {node_id}")
        mapped[node_id] = decision

    missing_ids = expected_ids - mapped.keys()
    if missing_ids:
        missing_joined = ", ".join(sorted(missing_ids))
        raise JobFailedError(f"검색 필터 응답에 누락된 node_id가 있습니다: {missing_joined}")

    return mapped


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _build_search_fingerprint(
    *,
    query_text: str,
    query_embedding: list[float],
    top_k: int,
    metadata: dict[str, object] | None,
) -> str:
    payload = {
        "query_text": " ".join(query_text.split()),
        "query_embedding": [round(float(value), 6) for value in query_embedding],
        "top_k": int(top_k),
        "metadata": metadata or {},
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
