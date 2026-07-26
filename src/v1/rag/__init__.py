"""RAG 서빙 전략 패키지.

본 __init__.py는 GPU·모델 의존성 없는 light 모듈만 re-export.
heavy 모듈(clients·search·sibling·tokens)은 import만 해도 CrossEncoder/AutoTokenizer
같은 큰 자원을 로드하므로, 사용처(router.py 등)가 submodule에서 직접 import하게 둠.
이렇게 분리해야 unit test가 `from src.v1.rag.trace import TraceRecord` 같은 가벼운
import만으로도 실행 가능 (host 에 모델 파일 없어도 안전).
"""

from .classifier import classify_query, RouteResult, SearchStrategy, QueryType
from .grader import evaluate_retrieval, verify_answer, classify_failure, build_hint, FailureType
from .prompts import PROMPTS, REWRITE_PROMPT, REGENERATE_WITH_HINT_PROMPT
from .trace import TraceRecord, trace_record, trace_span, get_trace, write_trace

__all__ = [
    "classify_query", "RouteResult", "SearchStrategy", "QueryType",
    "evaluate_retrieval", "verify_answer", "classify_failure", "build_hint", "FailureType",
    "PROMPTS", "REWRITE_PROMPT", "REGENERATE_WITH_HINT_PROMPT",
    "TraceRecord", "trace_record", "trace_span", "get_trace", "write_trace",
]
