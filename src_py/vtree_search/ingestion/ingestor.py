"""
목적:
- 문서 적재용 공개 클래스 `VtreeIngestor`를 제공한다.

설명:
- summary/page 노드 upsert와 summary 갱신 트리거를 Rust 적재 브릿지에 위임한다.
- 파일 기반 전처리(표/이미지 주석 포함)를 통해 page 노드를 생성하는 기능을 제공한다.

디자인 패턴:
- 서비스 레이어(Service Layer).

참조:
- src_py/vtree_search/runtime/bridge.py
- src_py/vtree_search/contracts/ingestion_models.py
- src_py/vtree_search/ingestion/source_parser.py
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vtree_search.config.models import IngestionConfig
from vtree_search.contracts.ingestion_models import (
    IngestionDocument,
    IngestionPageNode,
    IngestionResult,
    IngestionSummaryNode,
)
from vtree_search.exceptions import IngestionProcessingError
from vtree_search.ingestion.source_parser import build_source_parser
from vtree_search.ingestion.summary_state import RedisSummaryStateStore
from vtree_search.llm.langchain_ingestion import LangChainIngestionAnnotationLLM
from vtree_search.runtime.bridge import RustRuntimeBridge

_SUMMARY_TEXT_LIMIT = 4_000
_TITLE_TEXT_LIMIT = 120


@dataclass(frozen=True, slots=True)
class _SubchState:
    """sub-ch 누적 상태 모델."""

    tree_path: str
    chapter_path: str
    title: str
    summary_text: str
    evidence_pages: list[str]


@dataclass(frozen=True, slots=True)
class _ChState:
    """ch 누적 상태 모델."""

    tree_path: str
    title: str
    summary_text: str
    covered_subch: list[str]


@dataclass(frozen=True, slots=True)
class _SummarySnapshot:
    """summary_state 저장용 스냅샷 모델."""

    subch_states: list[_SubchState]
    ch_states: list[_ChState]
    doc_title: str
    doc_summary: str
    coverage_map: dict[str, list[str]]


class VtreeIngestor:
    """문서 적재용 엔진 클래스."""

    def __init__(
        self,
        config: IngestionConfig,
        llm=None,
        runtime_bridge: RustRuntimeBridge | None = None,
    ) -> None:
        self._config = config
        self._annotation_llm = (
            None if llm is None else LangChainIngestionAnnotationLLM(chat_model=llm)
        )
        self._runtime_bridge = runtime_bridge or RustRuntimeBridge()
        self._summary_state = (
            None if config.summary_state is None else RedisSummaryStateStore(config.summary_state)
        )

    async def upsert_document(self, document: IngestionDocument) -> IngestionResult:
        """문서 단위 summary/page 노드를 upsert한다."""
        payload = self._build_ingestion_payload(
            operation="upsert_document",
            document_id=document.document_id,
            summary_nodes=[node.model_dump() for node in document.summary_nodes],
            page_nodes=[node.model_dump() for node in document.page_nodes],
        )

        response = await asyncio.to_thread(self._runtime_bridge.execute_ingestion_job, payload)
        return IngestionResult.model_validate(response)

    async def upsert_pages(self, document_id: str, pages: list[IngestionPageNode]) -> IngestionResult:
        """페이지 노드만 upsert한다."""
        payload = self._build_ingestion_payload(
            operation="upsert_pages",
            document_id=document_id,
            summary_nodes=[],
            page_nodes=[node.model_dump() for node in pages],
        )

        response = await asyncio.to_thread(self._runtime_bridge.execute_ingestion_job, payload)
        return IngestionResult.model_validate(response)

    async def rebuild_summary_embeddings(self, document_id: str) -> IngestionResult:
        """summary 노드 갱신 트리거를 실행한다."""
        payload = self._build_ingestion_payload(
            operation="rebuild_summary_embeddings",
            document_id=document_id,
            summary_nodes=[],
            page_nodes=[],
        )

        response = await asyncio.to_thread(self._runtime_bridge.execute_ingestion_job, payload)
        return IngestionResult.model_validate(response)

    async def build_page_nodes_from_path(
        self,
        *,
        document_id: str,
        parent_node_id: str,
        input_root: str | Path,
        sample: bool | None = None,
    ) -> list[IngestionPageNode]:
        """입력 파일 루트에서 페이지 노드를 생성한다.

        Args:
            document_id: 대상 문서 ID.
            parent_node_id: 생성할 페이지 노드의 부모(summary) 노드 ID.
            input_root: 파싱 대상 루트 경로.
            sample: True면 확장자별 1개 파일만 처리.

        Returns:
            적재 가능한 `IngestionPageNode` 목록.
        """
        parser = build_source_parser(self._config, annotation_llm=self._annotation_llm)
        return await parser.build_page_nodes_from_files(
            document_id=document_id,
            parent_node_id=parent_node_id,
            input_root=input_root,
            sample=sample,
        )

    async def upsert_document_from_path(
        self,
        *,
        document_id: str,
        summary_nodes: list[IngestionSummaryNode],
        parent_node_id: str,
        input_root: str | Path,
        sample: bool | None = None,
        document_version: str = "v1",
    ) -> IngestionResult:
        """입력 파일에서 page 노드를 생성해 문서 단위로 업서트한다."""
        if not summary_nodes:
            raise IngestionProcessingError("summary_nodes는 최소 1개 이상이어야 합니다")

        version = document_version.strip() or "v1"
        run_id = uuid.uuid4().hex
        lock_token = uuid.uuid4().hex
        lock_acquired = False
        state_initialized = False

        if self._summary_state is not None:
            lock_acquired = await asyncio.to_thread(
                self._summary_state.acquire_writer_lock,
                document_id=document_id,
                version=version,
                lock_token=lock_token,
            )
            if not lock_acquired:
                raise IngestionProcessingError(
                    "동일 문서 버전의 summary_state writer 락을 획득하지 못했습니다: "
                    f"document_id={document_id}, version={version}"
                )

        try:
            page_nodes = await self.build_page_nodes_from_path(
                document_id=document_id,
                parent_node_id=parent_node_id,
                input_root=input_root,
                sample=sample,
            )
            summary_snapshot = _build_summary_snapshot(
                document_id=document_id,
                summary_nodes=summary_nodes,
                page_nodes=page_nodes,
            )

            if self._summary_state is not None:
                await asyncio.to_thread(
                    self._summary_state.initialize_run,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    lock_token=lock_token,
                    total_pages=len(page_nodes),
                    total_subch=len(summary_snapshot.subch_states),
                    total_ch=len(summary_nodes),
                )
                state_initialized = True

                await asyncio.to_thread(
                    self._summary_state.add_page_done,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    page_paths=[node.path for node in page_nodes],
                )

                await asyncio.to_thread(
                    self._summary_state.update_meta,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    stage="SUBCH",
                    status="RUNNING",
                )

                for revision, subch_state in enumerate(summary_snapshot.subch_states, start=1):
                    await asyncio.to_thread(
                        self._summary_state.upsert_subch_state,
                        document_id=document_id,
                        version=version,
                        run_id=run_id,
                        tree_path=subch_state.tree_path,
                        title=subch_state.title,
                        summary_text=subch_state.summary_text,
                        qa_topics=[],
                        evidence_pages=subch_state.evidence_pages,
                        source_page_count=len(subch_state.evidence_pages),
                        revision=revision,
                    )

                await asyncio.to_thread(
                    self._summary_state.update_meta,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    stage="CH",
                    status="RUNNING",
                    done_subch=len(summary_snapshot.subch_states),
                )

                for revision, ch_state in enumerate(summary_snapshot.ch_states, start=1):
                    await asyncio.to_thread(
                        self._summary_state.upsert_ch_state,
                        document_id=document_id,
                        version=version,
                        run_id=run_id,
                        tree_path=ch_state.tree_path,
                        title=ch_state.title,
                        summary_text=ch_state.summary_text,
                        qa_topics=[],
                        covered_subch=ch_state.covered_subch,
                        revision=revision,
                    )

                await asyncio.to_thread(
                    self._summary_state.update_meta,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    stage="DOC",
                    status="RUNNING",
                    done_subch=len(summary_snapshot.subch_states),
                    done_ch=len(summary_nodes),
                )

                await asyncio.to_thread(
                    self._summary_state.upsert_doc_state,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    title=summary_snapshot.doc_title,
                    summary_text=summary_snapshot.doc_summary,
                    qa_topics=[],
                    coverage_map=summary_snapshot.coverage_map,
                    revision=1,
                    status="DRAFT",
                )

            document = IngestionDocument(
                document_id=document_id,
                summary_nodes=summary_nodes,
                page_nodes=page_nodes,
            )
            result = await self.upsert_document(document)

            if self._summary_state is not None:
                await asyncio.to_thread(
                    self._summary_state.upsert_doc_state,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    title=summary_snapshot.doc_title,
                    summary_text=summary_snapshot.doc_summary,
                    qa_topics=[],
                    coverage_map=summary_snapshot.coverage_map,
                    revision=2,
                    status="FINAL",
                )
                await asyncio.to_thread(
                    self._summary_state.mark_succeeded,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                )

            return result
        except Exception as exc:
            if self._summary_state is not None and lock_acquired and state_initialized:
                await asyncio.to_thread(
                    self._summary_state.mark_failed,
                    document_id=document_id,
                    version=version,
                    run_id=run_id,
                    error_message=str(exc),
                )
            raise
        finally:
            if self._summary_state is not None and lock_acquired:
                await asyncio.to_thread(
                    self._summary_state.release_writer_lock,
                    document_id=document_id,
                    version=version,
                    lock_token=lock_token,
                )

    def _build_ingestion_payload(
        self,
        operation: str,
        document_id: str | None,
        summary_nodes: list[dict[str, Any]],
        page_nodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "document_id": document_id,
            "summary_nodes": summary_nodes,
            "page_nodes": page_nodes,
            "postgres": {
                "dsn": self._config.postgres.to_dsn(),
                "summary_table": self._config.postgres.summary_table,
                "page_table": self._config.postgres.page_table,
                "pool_min": self._config.postgres.pool_min,
                "pool_max": self._config.postgres.pool_max,
                "connect_timeout_ms": self._config.postgres.connect_timeout_ms,
                "statement_timeout_ms": self._config.postgres.statement_timeout_ms,
            },
        }


def _build_summary_snapshot(
    *,
    document_id: str,
    summary_nodes: list[IngestionSummaryNode],
    page_nodes: list[IngestionPageNode],
) -> _SummarySnapshot:
    chapter_title_map = _build_chapter_title_map(summary_nodes)
    page_ch_map = _build_page_chapter_map(summary_nodes=summary_nodes, page_nodes=page_nodes)
    subch_states = _build_subch_states(
        page_nodes=page_nodes,
        page_ch_map=page_ch_map,
        chapter_title_map=chapter_title_map,
    )
    subch_summary_map = {state.tree_path: state.summary_text for state in subch_states}
    subch_title_map = {state.tree_path: state.title for state in subch_states}

    ch_states: list[_ChState] = []
    for summary_node in summary_nodes:
        covered_subch = [
            state.tree_path
            for state in subch_states
            if state.chapter_path == summary_node.path
        ]
        ch_summary = _build_ch_summary(
            base_summary=summary_node.summary_text,
            covered_subch=covered_subch,
            subch_summary_map=subch_summary_map,
        )
        ch_states.append(
            _ChState(
                tree_path=summary_node.path,
                title=_build_ch_title(
                    summary_node=summary_node,
                    covered_subch=covered_subch,
                    subch_title_map=subch_title_map,
                ),
                summary_text=ch_summary,
                covered_subch=covered_subch,
            )
        )

    doc_summary = _build_doc_summary(
        ch_summaries=[state.summary_text for state in ch_states]
    )
    doc_title = _build_doc_title(
        document_id=document_id,
        ch_titles=[state.title for state in ch_states],
        doc_summary=doc_summary,
    )
    coverage_map = _build_coverage_map(
        summary_nodes=summary_nodes,
        page_nodes=page_nodes,
        page_ch_map=page_ch_map,
    )
    return _SummarySnapshot(
        subch_states=subch_states,
        ch_states=ch_states,
        doc_title=doc_title,
        doc_summary=doc_summary,
        coverage_map=coverage_map,
    )


def _build_chapter_title_map(
    summary_nodes: list[IngestionSummaryNode],
) -> dict[str, str]:
    title_map: dict[str, str] = {}
    for summary_node in summary_nodes:
        metadata = summary_node.metadata or {}
        metadata_title = str(metadata.get("title", "")).strip()
        summary_title = _derive_title_text(summary_node.summary_text)
        title_map[summary_node.path] = _clip_title(
            metadata_title or summary_title or _title_from_path(summary_node.path)
        )
    return title_map


def _build_page_chapter_map(
    *,
    summary_nodes: list[IngestionSummaryNode],
    page_nodes: list[IngestionPageNode],
) -> dict[str, str]:
    chapter_paths = [str(node.path) for node in summary_nodes]
    default_chapter = chapter_paths[0]
    sorted_chapter_paths = sorted(chapter_paths, key=len, reverse=True)
    page_ch_map: dict[str, str] = {}

    for page_node in page_nodes:
        page_path = str(page_node.path)
        matched_chapter = ""
        for chapter_path in sorted_chapter_paths:
            candidate_path = str(chapter_path)
            if page_path == candidate_path or page_path.startswith(f"{candidate_path}."):
                matched_chapter = candidate_path
                break
        selected_chapter = str(default_chapter)
        if matched_chapter:
            selected_chapter = str(matched_chapter)
        page_ch_map[page_path] = selected_chapter

    return page_ch_map


def _build_subch_states(
    *,
    page_nodes: list[IngestionPageNode],
    page_ch_map: dict[str, str],
    chapter_title_map: dict[str, str],
) -> list[_SubchState]:
    grouped_nodes: dict[str, list[IngestionPageNode]] = {}
    grouped_titles: dict[str, str] = {}
    chapter_counters: defaultdict[str, int] = defaultdict(int)
    current_subch_by_ch: dict[str, str] = {}
    ordered_subch_paths: list[str] = []

    for page_node in sorted(page_nodes, key=lambda node: node.path):
        chapter_path = page_ch_map[page_node.path]
        if _is_heading_node(page_node):
            next_index = chapter_counters[chapter_path] + 1
            chapter_counters[chapter_path] = next_index
            subch_path = f"{chapter_path}.sub{next_index:02d}"
            current_subch_by_ch[chapter_path] = subch_path
            ordered_subch_paths.append(subch_path)
            grouped_nodes[subch_path] = []
            grouped_titles[subch_path] = _build_subch_title(
                page_node=page_node,
                chapter_title=chapter_title_map[chapter_path],
                section_index=next_index,
            )

        subch_path = current_subch_by_ch.get(chapter_path)
        if subch_path is None:
            next_index = chapter_counters[chapter_path] + 1
            chapter_counters[chapter_path] = next_index
            subch_path = f"{chapter_path}.sub{next_index:02d}"
            current_subch_by_ch[chapter_path] = subch_path
            ordered_subch_paths.append(subch_path)
            grouped_nodes[subch_path] = []
            grouped_titles[subch_path] = _clip_title(
                f"{chapter_title_map[chapter_path]} 섹션 {next_index}"
            )

        grouped_nodes[subch_path].append(page_node)

    subch_states: list[_SubchState] = []
    for subch_path in ordered_subch_paths:
        chapter_path = _extract_parent_ch_path(subch_path)
        subch_nodes = grouped_nodes[subch_path]
        subch_summary = _build_subch_summary(subch_nodes)
        title = grouped_titles[subch_path]
        if not title.strip():
            title = _clip_title(
                _derive_title_text(subch_summary)
                or f"{chapter_title_map[chapter_path]} 섹션"
            )
        subch_states.append(
            _SubchState(
                tree_path=subch_path,
                chapter_path=chapter_path,
                title=title,
                summary_text=subch_summary,
                evidence_pages=[node.path for node in subch_nodes],
            )
        )

    return subch_states


def _is_heading_node(page_node: IngestionPageNode) -> bool:
    metadata = page_node.metadata or {}
    return str(metadata.get("block_type", "")).strip().lower() == "heading"


def _build_subch_title(
    *,
    page_node: IngestionPageNode,
    chapter_title: str,
    section_index: int,
) -> str:
    derived = _derive_title_text(page_node.content)
    if derived:
        return _clip_title(derived)
    return _clip_title(f"{chapter_title} 섹션 {section_index}")


def _build_ch_title(
    *,
    summary_node: IngestionSummaryNode,
    covered_subch: list[str],
    subch_title_map: dict[str, str],
) -> str:
    metadata = summary_node.metadata or {}
    title = str(metadata.get("title", "")).strip()
    if not title:
        title = _derive_title_text(summary_node.summary_text)
    if (not title or _is_code_style_title(title)) and covered_subch:
        first_subch_title = subch_title_map.get(covered_subch[0], "").strip()
        if first_subch_title:
            title = first_subch_title
    if not title:
        title = _title_from_path(summary_node.path)
    return _clip_title(title)


def _build_doc_title(
    *,
    document_id: str,
    ch_titles: list[str],
    doc_summary: str,
) -> str:
    normalized_titles = [title.strip() for title in ch_titles if title.strip()]
    if normalized_titles:
        if len(normalized_titles) == 1:
            return _clip_title(normalized_titles[0])
        return _clip_title(f"{normalized_titles[0]} 외 {len(normalized_titles) - 1}개 챕터")
    from_summary = _derive_title_text(doc_summary)
    if from_summary:
        return _clip_title(from_summary)
    return _clip_title(document_id)


def _extract_parent_ch_path(subch_path: str) -> str:
    parts = subch_path.rsplit(".", maxsplit=1)
    return parts[0] if len(parts) == 2 else subch_path


def _build_subch_summary(page_nodes: list[IngestionPageNode]) -> str:
    content_parts = [node.content.strip() for node in page_nodes if node.content.strip()]
    return _clip_text("\n\n".join(content_parts))


def _build_ch_summary(
    *,
    base_summary: str,
    covered_subch: list[str],
    subch_summary_map: dict[str, str],
) -> str:
    subch_parts = [subch_summary_map.get(path, "").strip() for path in covered_subch]
    subch_text = "\n\n".join(part for part in subch_parts if part)
    if base_summary.strip() and subch_text:
        return _clip_text(f"{base_summary.strip()}\n\n{subch_text}")
    if base_summary.strip():
        return _clip_text(base_summary.strip())
    return _clip_text(subch_text)


def _build_doc_summary(*, ch_summaries: list[str]) -> str:
    texts = [text.strip() for text in ch_summaries if text.strip()]
    return _clip_text("\n\n".join(texts))


def _build_coverage_map(
    *,
    summary_nodes: list[IngestionSummaryNode],
    page_nodes: list[IngestionPageNode],
    page_ch_map: dict[str, str],
) -> dict[str, list[str]]:
    grouped_pages: dict[str, list[str]] = {node.path: [] for node in summary_nodes}
    for page_node in sorted(page_nodes, key=lambda node: node.path):
        ch_path = page_ch_map[page_node.path]
        grouped_pages.setdefault(ch_path, []).append(page_node.path)

    coverage_map: dict[str, list[str]] = {}
    for summary_node in summary_nodes:
        coverage_map[summary_node.path] = grouped_pages.get(summary_node.path, [])
    return coverage_map


def _derive_title_text(value: str) -> str:
    first_line = value.splitlines()[0].strip() if value.splitlines() else ""
    normalized = " ".join(first_line.split()).strip()
    normalized = normalized.lstrip("#").strip()
    if not normalized:
        normalized = " ".join(value.split()).strip()
    if not normalized:
        return ""
    sentence_candidates = [". ", "? ", "! ", " - ", " | "]
    for separator in sentence_candidates:
        if separator in normalized:
            normalized = normalized.split(separator, maxsplit=1)[0].strip()
            break
    return _clip_title(normalized)


def _title_from_path(path: str) -> str:
    leaf = path.split(".")[-1].strip().replace("_", " ")
    return _clip_title(leaf or "제목 없음")


def _is_code_style_title(title: str) -> bool:
    compact = title.strip().replace("_", "").replace("-", "").lower()
    if not compact.startswith("ch"):
        return False
    suffix = compact[2:]
    return bool(suffix) and suffix.isdigit()


def _clip_text(value: str) -> str:
    if len(value) <= _SUMMARY_TEXT_LIMIT:
        return value
    return value[:_SUMMARY_TEXT_LIMIT]


def _clip_title(value: str) -> str:
    if len(value) <= _TITLE_TEXT_LIMIT:
        return value
    return value[:_TITLE_TEXT_LIMIT]
