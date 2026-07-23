"""API Router v1 — FastAPI endpoint 정의 + 의존성 주입 + PII guard.

비즈니스 로직(검색·sibling·토큰·검증·critic)은 rag/ 패키지에 위치 — router는 얇은 진입점.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from .config import get_db
from .config.settings import CRAG_MAX_RETRIES
from .guards import mask_pii, mask_pii_list, sanitize_input, sanitize_output
from .logger import api_logger
from .rag import (
    PROMPTS,
    REGENERATE_WITH_HINT_PROMPT,
    QueryType,
    build_hint,
    classify_failure,
    classify_query,
    evaluate_retrieval,
    trace_record,
    trace_span,
    verify_answer,
    write_trace,
)
from .rag.clients import invoke_clean
from .rag.search import (
    build_filter,
    format_sources,
    rewrite_query,
    search_and_rerank,
    search_comparison,
)
from .rag.sibling import expand_siblings
from .rag.tokens import calc_context_budget, count_tokens, truncate_context
from .repository import DocumentRepository, FeedbackRepository
from .schemas import (
    AnswerRequest,
    DocumentCreate,
    EmbedRequest,
    FeedbackRequest,
    FeedbackResponse,
    RetrieveRequest,
)
from .utils import embed_texts


# Feedback 수집 토글 — 점진적 롤아웃·인프라 장애 시 코드 변경 없이 비활성화.
FEEDBACK_ENABLED = os.environ.get("FEEDBACK_ENABLED", "true").lower() == "true"

# Critic dispatch 토글 — 기본 OFF (opt-in). 실측(2026-04-29 trace 27건): regenerate improved 14.3%(1/7),
# 발동 시 p95 latency 약 2배(5.6s→14.4s). 전문가 검토 툴 특성상 auto-regenerate보다 hard_fail 플래그 노출이 적합.
# 기본 동작(off): 모든 hard_fail/soft_fail이 그대로 응답에 노출 (escalation flag 없음).
# 근거·회고: docs/design-retrospective.md. 검색이 진짜 병목이라 측정될 때만 =true 로 켠다.
CRITIC_DISPATCH_ENABLED = os.environ.get("CRITIC_DISPATCH_ENABLED", "false").lower() == "true"


router = APIRouter()


def _verification_summary(verification: dict) -> dict:
    """rec.verification에 들어갈 슬림 dict + groundedness 0~1 점수.

    groundedness = supported / verifiable — RAGAS faithfulness · Azure AI Foundry ·
    Vectara HHEM 패턴. **검증 가능한 claim**(extracted_refs 또는 extracted_numerics가 있는)
    만 분모에 포함. 평문 claim ("이 경우 보험금이 지급됩니다")은 구조적으로
    supported_by_chunks가 강제 [] 이므로 분모에서 빼지 않으면 절차형 답변이 부당하게
    0점으로 깔리는 분모 결함이 생김. RAGAS도 "verifiable claims"만 분모로 둠.

    verifiable_claims_count==0 이면 groundedness 키 자체를 생략 — 측정 불가 (절차/해석
    답변에서 자연스럽게 발생). aggregator는 키 부재를 "no signal"로 처리 → 평균 왜곡 방지.
    """
    claims = verification["claims"]
    total = len(claims)
    verifiable = [c for c in claims if c["extracted_refs"] or c["extracted_numerics"]]
    supported = sum(1 for c in claims if c["supported_by_chunks"])
    out = {
        "risk_level": verification["risk_level"],
        "claims_count": total,
        "verifiable_claims_count": len(verifiable),
        "supported_claims_count": supported,
        "missing_refs_count": len(verification["missing_refs"]),
        "numeric_mismatch_count": len(verification["numeric_mismatches"]),
    }
    if verifiable:
        out["groundedness"] = round(supported / len(verifiable), 3)
    return out


def _apply_input_guard(body) -> tuple[list[str], list[str]]:
    """body의 query / include_keywords / exclude_keywords를 PII 마스킹 + injection sanitize.

    body를 in-place mutate해서 LLM·검색·trace 모두 정제된 텍스트만 사용.
    발견된 PII 종류 + injection 위협 라벨은 trace.input_guard에 기록 (raw 값은 저장 X).
    """
    body.query, q_kinds = mask_pii(body.query)
    body.query, threats = sanitize_input(body.query)
    inc_kinds: list[str] = []
    if body.include_keywords:
        body.include_keywords, inc_kinds = mask_pii_list(body.include_keywords)
    exc_kinds: list[str] = []
    if body.exclude_keywords:
        body.exclude_keywords, exc_kinds = mask_pii_list(body.exclude_keywords)
    return sorted(set(q_kinds + inc_kinds + exc_kinds)), threats


# ─────────────────────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/documents", summary="문서 등록", tags=["Documents"])
def create_document(doc: DocumentCreate, db: Session = Depends(get_db)):
    seqidx = DocumentRepository(db).create(
        doc.service_code, doc.document_id, doc.document_name, doc.document_path
    )

    try:
        from v1.task.extract import extract_pdf
        from v1.task.ocr import ocr_images
        from v1.task.chunk import chunk_document
        from v1.task.embed import embed_document

        result = (
            extract_pdf.s(doc.service_code, doc.document_id, doc.document_name)
            | ocr_images.s()
            | chunk_document.s()
            | embed_document.s()
        ).apply_async()

        api_logger.info(f"Task 발행 성공: {result.id}")
    except Exception as e:
        api_logger.error(f"Task 발행 실패: {e}", exc_info=True)

    api_logger.info(f"문서 등록: {doc.service_code}/{doc.document_id}")
    return {"id": seqidx, "message": "등록 완료"}


@router.get("/documents/{service_code}/{document_id}", summary="문서 상태 조회", tags=["Documents"])
def get_document_status(service_code: str, document_id: str, db: Session = Depends(get_db)):
    result = DocumentRepository(db).get_by_id(service_code, document_id)
    if not result:
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/retrieve", summary="벡터 검색", tags=["Retrieval"])
def retrieve(body: RetrieveRequest, background_tasks: BackgroundTasks):
    t0 = time.time()
    pii_found, injection_threats = _apply_input_guard(body)
    request_dict = body.model_dump(exclude_none=False)

    with trace_record("retrieve", request_dict) as rec:
        rec.input_guard = {
            "pii_found": pii_found,
            "pii_count": len(pii_found),
            "injection_threats": injection_threats,
        }
        try:
            route = classify_query(body.query)
            rec.route = {
                "strategy": route.strategy.value,
                "query_type": route.query_type.value,
                "dense_factor": route.dense_factor,
                "bm25_factor": route.bm25_factor,
            }

            query_filter = build_filter(
                service_code=body.service_code,
                document_id=body.document_id,
                start_page=body.start_page,
                end_page=body.end_page,
                include_keywords=body.include_keywords,
                exclude_keywords=body.exclude_keywords,
            )
            ranked = search_and_rerank(
                body.query, body.top_k, query_filter,
                dense_factor=route.dense_factor, bm25_factor=route.bm25_factor,
            )

            if not ranked:
                elapsed_ms = round((time.time() - t0) * 1000)
                rec.retrieval = {"result_count": 0, "chunk_ids": [], "rerank_scores": [], "rerank_stats": None}
                rec.timing_ms["total"] = elapsed_ms
                background_tasks.add_task(write_trace, rec)
                # trace_id·route는 검색 0건 케이스에도 항상 노출 (api.md 계약 + smoke test).
                return {"trace_id": rec.trace_id,
                        "query": body.query, "total": 0, "elapsed_ms": elapsed_ms, "sources": [],
                        "route": {"strategy": route.strategy.value, "query_type": route.query_type.value}}

            scores_list = [round(float(s), 4) for _, s in ranked]
            rec.retrieval = {
                "result_count": len(ranked),
                "chunk_ids": [str(r[0].id) for r in ranked],
                "rerank_scores": scores_list,
                "rerank_stats": {
                    "min": min(scores_list), "max": max(scores_list),
                    "mean": round(sum(scores_list) / len(scores_list), 4),
                },
            }

            sources = format_sources(ranked)
            with trace_span("sibling_expand"):
                context = expand_siblings(ranked)
            section_count = context.count("\n\n---\n\n") + 1 if context else 0
            rec.sibling = {"expanded_section_count": section_count, "total_chars": len(context)}

            elapsed_ms = round((time.time() - t0) * 1000)
            rec.timing_ms["total"] = elapsed_ms

            background_tasks.add_task(write_trace, rec)
            return {
                "trace_id": rec.trace_id,
                "query": body.query, "total": len(sources), "elapsed_ms": elapsed_ms,
                "context": context, "sources": sources,
                "route": {"strategy": route.strategy.value, "query_type": route.query_type.value},
            }

        except Exception as e:
            rec.error = {"type": type(e).__name__, "message": str(e)}
            rec.timing_ms["total"] = round((time.time() - t0) * 1000)
            background_tasks.add_task(write_trace, rec)
            raise


@router.post("/answer", summary="RAG 질의응답", tags=["Retrieval"])
def answer(body: AnswerRequest, background_tasks: BackgroundTasks):
    t0 = time.time()
    pii_found, injection_threats = _apply_input_guard(body)
    request_dict = body.model_dump(exclude_none=False)

    with trace_record("answer", request_dict) as rec:
        rec.input_guard = {
            "pii_found": pii_found,
            "pii_count": len(pii_found),
            "injection_threats": injection_threats,
        }
        try:
            route = classify_query(body.query)
            rec.route = {
                "strategy": route.strategy.value,
                "query_type": route.query_type.value,
                "dense_factor": route.dense_factor,
                "bm25_factor": route.bm25_factor,
            }
            api_logger.info(f"쿼리 라우팅: strategy={route.strategy.value}, type={route.query_type.value}")

            query_filter = build_filter(
                service_code=body.service_code,
                document_id=body.document_id,
                start_page=body.start_page,
                end_page=body.end_page,
                include_keywords=body.include_keywords,
                exclude_keywords=body.exclude_keywords,
            )

            current_query = body.query
            retry_count = 0

            if route.query_type == QueryType.COMPARISON:
                ranked = search_comparison(body.query, body.top_k, query_filter, route)
            else:
                ranked = search_and_rerank(
                    current_query, body.top_k, query_filter,
                    dense_factor=route.dense_factor, bm25_factor=route.bm25_factor,
                )

            initial_score = float(ranked[0][1]) if ranked else None
            rec.crag["attempts"].append({
                "attempt": 0, "top_score": initial_score, "rewritten_query": None,
            })
            rec.crag["score_before"] = initial_score

            # 재시도 시 rewrite로 query_type이 바뀌어도 원래 라우팅 전략 유지.
            original_route = route
            while not evaluate_retrieval(ranked) and retry_count < CRAG_MAX_RETRIES:
                retry_count += 1
                api_logger.info(f"CRAG 재검색 {retry_count}/{CRAG_MAX_RETRIES}")
                current_query = rewrite_query(current_query)
                if original_route.query_type == QueryType.COMPARISON:
                    ranked = search_comparison(current_query, body.top_k, query_filter, original_route)
                else:
                    ranked = search_and_rerank(
                        current_query, body.top_k, query_filter,
                        dense_factor=original_route.dense_factor,
                        bm25_factor=original_route.bm25_factor,
                    )
                rec.crag["attempts"].append({
                    "attempt": retry_count,
                    "top_score": float(ranked[0][1]) if ranked else None,
                    "rewritten_query": current_query,
                })

            rec.crag["retries"] = retry_count
            rec.crag["score_after"] = float(ranked[0][1]) if ranked else None

            if not ranked:
                elapsed_ms = round((time.time() - t0) * 1000)
                rec.retrieval = {
                    "result_count": 0, "chunk_ids": [],
                    "rerank_scores": [], "rerank_stats": None,
                }
                rec.answer = {"length_chars": 0, "is_refusal": True}
                rec.timing_ms["total"] = elapsed_ms
                background_tasks.add_task(write_trace, rec)
                # route는 검색 전에 이미 일어난 일이므로 0건 케이스에도 항상 노출 (api.md 계약).
                return {"trace_id": rec.trace_id,
                        "query": body.query, "answer": "관련 내용을 찾지 못했습니다.",
                        "elapsed_ms": elapsed_ms, "sources": [],
                        "route": {"strategy": route.strategy.value, "query_type": route.query_type.value}}

            scores_list = [round(float(s), 4) for _, s in ranked]
            rec.retrieval = {
                "result_count": len(ranked),
                "chunk_ids": [str(r[0].id) for r in ranked],
                "rerank_scores": scores_list,
                "rerank_stats": {
                    "min": min(scores_list), "max": max(scores_list),
                    "mean": round(sum(scores_list) / len(scores_list), 4),
                },
            }

            with trace_span("sibling_expand"):
                context = expand_siblings(ranked)
            section_count = context.count("\n\n---\n\n") + 1 if context else 0
            rec.sibling = {"expanded_section_count": section_count, "total_chars": len(context)}

            prompt = PROMPTS[route.query_type]
            system_text = prompt.format_messages(context="", query=body.query)[0].content
            budget = calc_context_budget(system_text, body.query)

            with trace_span("context_truncate"):
                context_before_len = len(context)
                context = truncate_context(context, budget)
            rec.context = {
                "truncated": len(context) < context_before_len,
                "token_budget": budget,
                "final_tokens": count_tokens(context),
            }

            with trace_span("llm_generate"):
                answer_text = invoke_clean(prompt.format_messages(context=context, query=body.query))

            # Output Guard — role token leak / 욕설 정제. leak 토큰은 silent 제거.
            answer_text, output_threats = sanitize_output(answer_text)

            # Chunk-level provenance: 리랭킹 결과의 chunk id/content를 verifier에 전달해 claim-근거 매핑 생성.
            from .rag.grader import Chunk as VerifyChunk
            verify_chunks = [
                VerifyChunk(id=str(r[0].id), content=(r[0].payload or {}).get("content", ""))
                for r in ranked
            ]
            with trace_span("verify"):
                verification = verify_answer(answer_text, context=context, chunks=verify_chunks)

            rec.verification = _verification_summary(verification)

            if verification["risk_level"] == "hard_fail":
                api_logger.warning(f"Self-RAG 검증 실패[hard_fail]: {verification['warnings']}")
            elif verification["risk_level"] == "soft_fail":
                api_logger.warning(f"Self-RAG 검증 경고[soft_fail]: {verification['warnings']}")

            # Critic dispatch — failure_type별로 regenerate / escalate / pass 분기.
            # retrieval_gap·semantic_mismatch는 regenerate 금지 (Huang et al. ICLR 2024 자기교정 함정).
            # semantic_judge 미주입 상태라 semantic_mismatch는 현재 발동 안 함.
            escalation_required = False
            if CRITIC_DISPATCH_ENABLED and verification["risk_level"] in ("hard_fail", "soft_fail"):
                failure_type = classify_failure(verification, context, answer=answer_text)
                before_risk = verification["risk_level"]
                action_taken = "pass"
                regenerate_improved: bool | None = None

                if failure_type in ("generation_error", "unit_error"):
                    hint = build_hint(failure_type, verification, context)
                    with trace_span("regenerate"):
                        answer_text = invoke_clean(
                            REGENERATE_WITH_HINT_PROMPT.format_messages(
                                context=context, query=body.query, hint=hint,
                            )
                        )
                    answer_text, regen_threats = sanitize_output(answer_text)
                    output_threats = output_threats + regen_threats
                    verification = verify_answer(answer_text, context=context, chunks=verify_chunks)
                    action_taken = "regenerate"
                    regenerate_improved = verification["risk_level"] == "pass"
                    api_logger.info(
                        f"Critic regenerate: {failure_type} → {verification['risk_level']} "
                        f"(improved={regenerate_improved})"
                    )
                    rec.verification = _verification_summary(verification)
                elif failure_type in ("retrieval_gap", "semantic_mismatch"):
                    action_taken = "escalate"
                    escalation_required = True
                    api_logger.warning(
                        f"Critic escalation: {failure_type}, regenerate 금지 "
                        f"(missing={verification['missing_refs'][:3]})"
                    )
                # minor는 위 두 분기에 안 잡힌 채 action_taken="pass" 그대로 유지.

                rec.critic = {
                    "invoked": True,
                    "failure_type": failure_type,
                    "action_taken": action_taken,
                    "before_risk": before_risk,
                    "after_risk": verification["risk_level"],
                    "regenerate_improved": regenerate_improved,
                }
            else:
                rec.critic = {"invoked": False}

            is_refusal = (
                "확인되지 않음" in answer_text
                or "관련 내용을 찾지 못했습니다" in answer_text
            )
            rec.answer = {"length_chars": len(answer_text), "is_refusal": is_refusal}
            rec.output_guard = {"threats": output_threats}

            elapsed_ms = round((time.time() - t0) * 1000)
            rec.timing_ms["total"] = elapsed_ms

            sources = format_sources(ranked)
            # Citation projection — verify_answer가 이미 산출한 claim-chunk 매핑을 응답에 노출.
            # supported_by_chunks 비어있으면 인용 매핑 불가(no_refs claim) → 노출 제외.
            citations = [
                {
                    "claim": c["text"],
                    "refs": c.get("extracted_refs", []),
                    "supported_by_chunks": c.get("supported_by_chunks", []),
                }
                for c in verification["claims"]
                if c.get("supported_by_chunks")
            ]
            result = {
                "trace_id": rec.trace_id,
                "query": body.query, "answer": answer_text,
                "elapsed_ms": elapsed_ms, "sources": sources,
                "route": {"strategy": route.strategy.value, "query_type": route.query_type.value},
            }
            if citations:
                result["citations"] = citations
            if verification["warnings"] or escalation_required:
                result["verification"] = {
                    "risk_level": verification["risk_level"],
                    "groundedness": rec.verification["groundedness"],
                    "warnings": verification["warnings"],
                }
                if escalation_required:
                    # retrieval_gap / semantic_mismatch — 클라이언트가 재질문 유도·refusal UI 등으로 활용.
                    result["verification"]["escalation_required"] = True
            if retry_count > 0:
                result["crag_retries"] = retry_count

            background_tasks.add_task(write_trace, rec)
            return result

        except Exception as e:
            rec.error = {"type": type(e).__name__, "message": str(e)}
            rec.timing_ms["total"] = round((time.time() - t0) * 1000)
            background_tasks.add_task(write_trace, rec)
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Embeddings · Feedback
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/embeddings", summary="텍스트 → 벡터 변환", tags=["Embeddings"])
def embed_text(body: EmbedRequest):
    vectors = embed_texts(body.texts)
    return {
        "total": len(vectors),
        "dimension": len(vectors[0]) if vectors else 0,
        "vectors": vectors,
    }


@router.post("/feedback", summary="쿼리 피드백 수집", tags=["Feedback"], response_model=FeedbackResponse)
def feedback(body: FeedbackRequest, db: Session = Depends(get_db)):
    """trace_id 기반 사용자 피드백 수집 (Insert-only).

    trace_id 실존 검증은 안 함 — 매 요청 JSONL I/O 회피 + BackgroundTasks trace write와의
    race 방지. 매칭률은 trace_summary.py --feedback이 사후 모니터링.
    """
    if not FEEDBACK_ENABLED:
        raise HTTPException(503, detail="Feedback 수집 일시 중단")

    try:
        fb = FeedbackRepository(db).insert(
            trace_id=body.trace_id,
            signal=body.signal,
            free_text=body.free_text,
        )
        db.commit()
        api_logger.info(f"Feedback: trace_id={body.trace_id} signal={body.signal}")
        return FeedbackResponse(
            id=fb.id,
            stored_at=fb.created_at.isoformat() if fb.created_at else "",
        )
    except Exception as e:
        db.rollback()
        api_logger.error(f"Feedback 저장 실패: {e}", exc_info=True)
        raise HTTPException(500, detail="Feedback 저장 중 오류")
