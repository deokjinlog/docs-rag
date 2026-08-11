"""검색 파이프라인 — 필터 빌드 / Hybrid (Dense+BM25+RRF) / 리랭킹 / 응답 포맷.

각 함수는 단일 책임 — `build_filter` 는 Qdrant Filter만, `search_rrf_only` 는 검색만,
`search_and_rerank` 는 검색+리랭킹. LLM helper(`rewrite_query`) 도 검색 context에서만 쓰여 동거.
"""
from __future__ import annotations

from qdrant_client.models import (
    Document as QdrantDocument,
    FieldCondition,
    Filter,
    FusionQuery,
    MatchText,
    MatchValue,
    Prefetch,
    QuantizationSearchParams,
    Range,
    SearchParams,
)

from ..config import BM25_CONFIG, QDRANT_CONFIG
from ..config.settings import SEARCH_PREFETCH_MULTIPLIER
from ..logger import api_logger
from ..utils import embed_query
from .clients import invoke_clean, qdrant, reranker
from .prompts import REWRITE_PROMPT
from .trace import get_trace, trace_span


def build_filter(
    service_code: str | None = None,
    document_id: str | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
    include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
) -> Filter | None:
    """Qdrant Filter 생성. 모든 필터 None이면 None 반환."""
    must = []
    if service_code:
        must.append(FieldCondition(key="service_code", match=MatchValue(value=service_code)))
    if document_id:
        must.append(FieldCondition(key="document_id", match=MatchValue(value=document_id)))
    if start_page is not None:
        must.append(FieldCondition(key="page_range[0]", range=Range(gte=start_page)))
    if end_page is not None:
        must.append(FieldCondition(key="page_range[1]", range=Range(lte=end_page)))
    if include_keywords:
        for kw in include_keywords:
            must.append(FieldCondition(key="content", match=MatchText(text=kw)))

    must_not = [FieldCondition(key="content", match=MatchText(text=kw)) for kw in (exclude_keywords or [])]

    if must or must_not:
        return Filter(must=must, must_not=must_not if must_not else None)
    return None


def search_rrf_only(
    query: str,
    top_k: int,
    query_filter: Filter | None = None,
    dense_factor: int = 6,
    bm25_factor: int = 6,
) -> list:
    """Hybrid 검색 — Dense (BGE-M3) + BM25 → RRF 융합. 리랭킹 없이 RRF score로 반환."""
    with trace_span("query_embed"):
        query_vector = embed_query(query)
    with trace_span("qdrant_search"):
        results = qdrant.query_points(
            collection_name=QDRANT_CONFIG["collection_name"],
            prefetch=[
                Prefetch(query=query_vector, using="dense",
                         limit=top_k * dense_factor, filter=query_filter),
                Prefetch(query=QdrantDocument(text=query, model="Qdrant/bm25"),
                         using=BM25_CONFIG["sparse_vector_name"],
                         limit=top_k * bm25_factor, filter=query_filter),
            ],
            query=FusionQuery(fusion="rrf"),
            limit=top_k * SEARCH_PREFETCH_MULTIPLIER,
            with_payload=True,
            search_params=SearchParams(
                hnsw_ef=QDRANT_CONFIG["hnsw_ef"],
                quantization=QuantizationSearchParams(rescore=True, oversampling=2.0),
            ),
        )
    return results.points if results.points else []


def search_and_rerank(
    query: str,
    top_k: int,
    query_filter: Filter | None = None,
    dense_factor: int = 6,
    bm25_factor: int = 6,
):
    """Hybrid 검색 + CrossEncoder 리랭킹. dense_factor/bm25_factor로 후보 풀 비중 조절."""
    points = search_rrf_only(query, top_k, query_filter, dense_factor, bm25_factor)
    if not points:
        return []

    pairs = [(query, _rerank_text(r.payload)) for r in points]
    with trace_span("rerank"):
        scores = reranker.predict(pairs)
    return sorted(zip(points, scores), key=lambda x: x[1], reverse=True)[:top_k]


def _rerank_text(payload: dict) -> str:
    """리랭커 입력을 임베딩 텍스트와 동일하게(heading_path + content) 구성.
    조 제목(예: '제15조 【특약의 갱신】')이 질의어와 매칭되도록 — 덴스 벡터는 이미 이 포맷이라
    (embed.py `_embed_text`) 리랭커만 content-only면 heading 신호가 최종 순위에 반영되지 않는다.
    실측: 청킹 heading 복구 후에도 '갱신' 질의가 rank2에 머문 원인이 이 불일치였다."""
    heading_path = payload.get("heading_path") or ""
    content = payload.get("content", "")
    return f"{heading_path}\n\n{content}" if heading_path else content


def rewrite_query(query: str) -> str:
    """LLM으로 쿼리 재작성 — CRAG 루프에서 검색 품질 낮을 때 호출."""
    rewritten = invoke_clean(REWRITE_PROMPT.format_messages(query=query))
    api_logger.info(f"CRAG 쿼리 재작성: '{query}' → '{rewritten}'")
    return rewritten


def format_sources(ranked: list) -> list[dict]:
    """Qdrant point + rerank score → API 응답용 dict 리스트.

    chunk_id는 응답의 citations[].supported_by_chunks가 sources를 lookup하는 키 —
    클라이언트가 inline citation UI 만들 때 매핑 인덱싱용.
    """
    sources = []
    for r, s in ranked:
        item = {
            "chunk_id": str(r.id),
            "document_id": r.payload.get("document_id"),
            "page_range": r.payload.get("page_range"),
            "content": r.payload.get("content"),
            "chunk_type": r.payload.get("chunk_type"),
            "rrf_score": round(r.score, 4),
            "rerank_score": round(float(s), 4),
        }
        image_paths = r.payload.get("image_paths")
        if image_paths:
            item["image_paths"] = image_paths
        sources.append(item)
    return sources
