# Python 모듈 레퍼런스

## 관련 문서

- [프로젝트 개요](../../README.md)
- [Python 개요](./README.md)
- [Python LLM 주입](./llm_injection.md)
- [아키텍처 청사진](../arch/blueprint.md)
- [Rust 모듈 레퍼런스](../rust/module_reference.md)
- [운영 가이드](../ops/queueing-and-slo.md)

## `vtree_search/__init__.py`

- 공개 클래스: `VtreeIngestor`, `VTreeSearchEngine`
- 공개 설정: `PostgresConfig`, `RedisQueueConfig`, `SearchConfig`, `IngestionConfig`, `SummaryStateConfig`
- 공개 LLM 어댑터: `LangChainSearchFilterLLM`, `LangChainIngestionAnnotationLLM`

## `vtree_search/config/models.py`

- `PostgresConfig`
- `RedisQueueConfig`
- `SearchConfig`
- `IngestionPreprocessConfig`
- `SummaryStateConfig`
- `IngestionConfig`

## `vtree_search/llm/contracts.py`

- `SearchFilterCandidate`
- `SearchFilterDecision`

## `vtree_search/llm/langchain_search.py`

- `LangChainSearchFilterLLM.filter()`

## `vtree_search/llm/langchain_ingestion.py`

- `LangChainIngestionAnnotationLLM.annotate_table()`
- `LangChainIngestionAnnotationLLM.annotate_image()`

## `vtree_search/contracts/search_models.py`

- `SearchSubmission`

## `vtree_search/contracts/job_models.py`

- `SearchJobAccepted`
- `SearchJobStatus`
- `SearchJobResult`
- `SearchJobCanceled`
- `SearchCandidate`
- `SearchMetrics`

## `vtree_search/contracts/ingestion_models.py`

- `IngestionSummaryNode`
- `IngestionPageNode`
- `IngestionDocument`
- `IngestionResult`

## `vtree_search/runtime/bridge.py`

- `RustRuntimeBridge.execute_search_job(payload)`
- `RustRuntimeBridge.execute_ingestion_job(payload)`
- `RustRuntimeBridge.search_status()`
- `RustRuntimeBridge.ingestion_status()`

## `vtree_search/queue/redis_streams.py`

- `RedisSearchQueue`
  - `guard_capacity()`
  - `create_job_record()`
  - `enqueue()`
  - `read()`
  - `ack()`
  - `move_to_dlq()`
  - `mark_*()` 상태 전이 메서드군

## `vtree_search/search/engine.py`

- `VTreeSearchEngine.submit_search()`
- `VTreeSearchEngine.get_job()`
- `VTreeSearchEngine.fetch_result()`
- `VTreeSearchEngine.cancel_job()`
- `VTreeSearchEngine.run_worker_once()` (`async`)
- `VTreeSearchEngine.run_worker_forever()` (`async`)
- 생성자 입력: `llm`에 LangChain 채팅 모델을 직접 전달한다.

## `vtree_search/ingestion/ingestor.py`

- `VtreeIngestor.upsert_document()` (`async`)
- `VtreeIngestor.upsert_pages()` (`async`)
- `VtreeIngestor.rebuild_summary_embeddings()` (`async`)
- `VtreeIngestor.build_page_nodes_from_path()` (`async`)
- `VtreeIngestor.upsert_document_from_path()` (`async`)
- 핵심 동작: 헤딩 기반 sub-ch 생성 + title 생성 + `summary_state` 누적

## `vtree_search/ingestion/summary_state.py`

- `RedisSummaryStateStore`
  - `acquire_writer_lock()`
  - `release_writer_lock()`
  - `initialize_run()`
  - `add_page_done()`
  - `upsert_subch_state()`
  - `upsert_ch_state()`
  - `upsert_doc_state()`
  - `mark_failed()`
  - `mark_succeeded()`

## `vtree_search/ingestion/source_parser.py`

- `SourceParser.scan_input_files()`
- `SourceParser.build_page_nodes_from_files()` (`async`)
- `build_source_parser()`

## `vtree_search/ingestion/prompts/`

- `table_prompt.py`: `TABLE_PROMPT`
- `image_prompt.py`: `IMAGE_PROMPT`

## `vtree_search/ingestion/docx_layout.py`

- `iterate_docx_blocks()`
- `resolve_docx_layout_metrics()`
- `estimate_docx_paragraph_layout()`
- `estimate_docx_table_height()`
- `advance_docx_page_state()`
- `is_docx_page_break_before()`

## `vtree_search/ingestion/parser_helpers.py`

- `serialize_docx_table()`
- `table_matrix_to_html()`
- `resolve_docx_heading_level()`
- `extract_pdf_image_boxes()`
- `to_pixel_box()`
- `chunk_blocks()`
