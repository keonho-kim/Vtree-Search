"""
목적:
- Vtree Search Python 패키지의 공개 진입점을 제공한다.

설명:
- 라이브러리 핵심 클래스는 `VtreeIngestor`, `VTreeSearchEngine` 두 가지다.
- 설정/인터페이스/예외/LLM 어댑터를 함께 노출한다.

디자인 패턴:
- 퍼사드(Facade).

참조:
- src_py/vtree_search/ingestion/ingestor.py
- src_py/vtree_search/search/engine.py
"""

from .config.models import (
    IngestionConfig,
    IngestionPreprocessConfig,
    PostgresConfig,
    RedisQueueConfig,
    SearchConfig,
    SummaryStateConfig,
)
from .contracts.ingestion_models import (
    IngestionDocument,
    IngestionPageNode,
    IngestionResult,
    IngestionSummaryNode,
)
from .contracts.job_models import (
    SearchCandidate,
    SearchJobAccepted,
    SearchJobCanceled,
    SearchJobResult,
    SearchJobStatus,
    SearchMetrics,
)
from .exceptions import (
    ConfigurationError,
    DependencyUnavailableError,
    IngestionProcessingError,
    JobExpiredError,
    JobFailedError,
    JobNotFoundError,
    QueueOverloadedError,
    VtreeSearchError,
)
from .ingestion.ingestor import VtreeIngestor
from .llm import (
    LangChainIngestionAnnotationLLM,
    LangChainSearchFilterLLM,
    SearchFilterCandidate,
    SearchFilterDecision,
)
from .search.engine import VTreeSearchEngine
from .version import __version__

__all__ = [
    "__version__",
    "VTreeSearchEngine",
    "VtreeIngestor",
    "SearchConfig",
    "IngestionConfig",
    "PostgresConfig",
    "RedisQueueConfig",
    "IngestionPreprocessConfig",
    "SummaryStateConfig",
    "SearchFilterCandidate",
    "SearchFilterDecision",
    "LangChainSearchFilterLLM",
    "LangChainIngestionAnnotationLLM",
    "SearchJobAccepted",
    "SearchJobStatus",
    "SearchJobResult",
    "SearchJobCanceled",
    "SearchCandidate",
    "SearchMetrics",
    "IngestionDocument",
    "IngestionSummaryNode",
    "IngestionPageNode",
    "IngestionResult",
    "VtreeSearchError",
    "ConfigurationError",
    "QueueOverloadedError",
    "JobNotFoundError",
    "JobExpiredError",
    "JobFailedError",
    "DependencyUnavailableError",
    "IngestionProcessingError",
]
