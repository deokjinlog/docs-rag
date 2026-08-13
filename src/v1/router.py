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
)
from .rag.sibling import expand_siblings
from .rag.tokens import calc_context_budget, count_tokens, truncate_context
from .repository import (
    DocumentRepository, FeedbackRepository, PayoutRepository, ProductRepository, CoverageRepository,
)
from .rag.payout_sql import (
    select_payout, format_payout, format_payout_complete, is_payout_amount_query,
)
from .rag.terms_sql import is_terms_query, coverage_hint, format_terms
from .rag.coverage_sql import (
    is_coverage_query, extract_code, extract_coverage, judge_coverage, format_coverage,
    effective_coverage,
)
from .rag.exclusion_sql import is_exclusion_query, format_exclusions
from .schemas import (
    AnswerRequest,
    CoverageRequest,
    DocumentCreate,
    EmbedRequest,
    ExclusionRequest,
    FeedbackRequest,
    FeedbackResponse,
    PayoutRequest,
    RetrieveRequest,
    TermsRequest,
)
from .utils import embed_texts


# Feedback 수집 토글 — 점진적 롤아웃·인프라 장애 시 코드 변경 없이 비활성화.
FEEDBACK_ENABLED = os.environ.get("FEEDBACK_ENABLED", "true").lower() == "true"

# Critic dispatch 토글 — 기본 OFF (opt-in). 실측(2026-04-29 trace 27건): regenerate improved 14.3%(1/7),
# 발동 시 p95 latency 약 2배(5.6s→14.4s). 전문가 검토 툴 특성상 auto-regenerate보다 hard_fail 플래그 노출이 적합.
# 기본 동작(off): 모든 hard_fail/soft_fail이 그대로 응답에 노출 (escalation flag 없음).
# 근거·회고: docs/design-retrospective.md. 검색이 진짜 병목이라 측정될 때만 =true 로 켠다.
CRITIC_DISPATCH_ENABLED = os.environ.get("CRITIC_DISPATCH_ENABLED", "false").lower() == "true"

# SQL 경로 자동 라우팅 (B5) — /answer가 "얼마/지급률" 질의를 payout_rule에서 결정론으로 답한다.
# 기본 on(검증됨: amount 게이트 precision + payout 골든 5/5). amount 게이트 통과 + select_payout
# hit여야 발동(2중 안전), 아니면 RAG 그대로. 문제 시 SQL_ROUTE_ENABLED=false로 즉시 끔.
SQL_ROUTE_ENABLED = os.environ.get("SQL_ROUTE_ENABLED", "true").lower() == "true"


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
    # include/exclude_keywords는 Retrieve/Answer body에만 있음 — /payout 등 키워드 없는
    # body와도 공유하려 getattr 방어(없으면 스킵).
    inc_kinds: list[str] = []
    if getattr(body, "include_keywords", None):
        body.include_keywords, inc_kinds = mask_pii_list(body.include_keywords)
    exc_kinds: list[str] = []
    if getattr(body, "exclude_keywords", None):
        body.exclude_keywords, exc_kinds = mask_pii_list(body.exclude_keywords)
    return sorted(set(q_kinds + inc_kinds + exc_kinds)), threats


def _coverage_answer_reconciled(db, product_id: str | None, code: str | None, verdict: dict | None) -> str:
    """보장판정 + payout **정합 조립**(reconcile) — 완벽한 답은 모순 없음. 미보장이면 실제
    담보로 리다이렉트해 그 담보의 payout을 붙인다("암진단자금 미보장 → 제자리암 10%").
    보장이면 그 담보 payout. domain-model.md 사실 reconciliation(제외 우선).
    """
    answer = format_coverage(code, verdict)
    eff = effective_coverage(verdict)
    if eff:
        prule = select_payout(PayoutRepository(db).get_rules(product_id), f"{eff} 얼마 지급")
        if prule:
            answer += f"  ※ {eff} 지급: {format_payout(prule)}"
    return answer


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
def answer(body: AnswerRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
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

            # SQL 경로 자동 라우팅 (B5) — "얼마/지급률" 결정론 질의는 payout_rule에서 집어온다.
            # amount 게이트(지급액 질의만) + select_payout hit 둘 다여야 발동, 아니면 RAG 그대로
            # (담보만 겹치는 해석 질의는 게이트가, 담보/규칙 미매칭은 hit=None이 막는 2중 안전).
            if SQL_ROUTE_ENABLED and is_payout_amount_query(body.query):
                _repo = PayoutRepository(db)
                _rule = select_payout(_repo.get_rules(), body.query)
                if _rule is not None:
                    # 면책 강제첨부 — "얼마?" 답에 지급 제외(면책)를 항상 붙인다(완결성).
                    _excl = _repo.get_exclusions(_rule["product_id"], _rule.get("coverage"))
                    _answer = format_payout_complete(_rule, _excl)
                    rec.route = {**(rec.route or {}), "strategy": "sql"}
                    api_logger.info(f"SQL 경로 적중: {_rule.get('coverage')} → {_rule.get('rate_pct')}%")
                    return {
                        "trace_id": rec.trace_id,
                        "query": body.query,
                        "answer": _answer,
                        "elapsed_ms": int((time.time() - t0) * 1000),
                        "sources": [],
                        "route": {"strategy": "sql", "query_type": route.query_type.value},
                        "citations": [{
                            "claim": _answer,
                            "refs": [r for r in (_rule.get("product_id"), _rule.get("coverage")) if r],
                            "supported_by_chunks": [],
                        }],
                    }

            # SQL 경로 — 계약조건("언제까지?": 청약철회·갱신). terms 게이트 + 담보 키워드로 상품
            # 해소되면 결정론(준용 NULL 포함), 아니면 RAG. amount(payout)와 배타.
            if SQL_ROUTE_ENABLED and is_terms_query(body.query):
                # document_id(R01)는 product_id(LINA_ICU)가 아니라 담보 키워드로만 상품 해소
                _hint = coverage_hint(body.query)
                _product = ProductRepository(db).get_terms(None, _hint) if _hint else None
                if _product is not None:
                    _tanswer = format_terms(_product)
                    rec.route = {**(rec.route or {}), "strategy": "sql"}
                    api_logger.info(f"SQL 경로(terms) 적중: {_product.get('product_id')}")
                    return {
                        "trace_id": rec.trace_id,
                        "query": body.query,
                        "answer": _tanswer,
                        "elapsed_ms": int((time.time() - t0) * 1000),
                        "sources": [],
                        "route": {"strategy": "sql", "query_type": route.query_type.value},
                        "citations": [{"claim": _tanswer,
                                       "refs": [_product.get("product_id")],
                                       "supported_by_chunks": []}],
                    }

            # SQL 경로 — 보장판정("이 병 보장돼요?": 별표3 ICD). coverage 게이트 + 코드 특정되면
            # 결정론 3-값 판정, 아니면 RAG(병명→코드 못 짚으면 RAG 소관).
            if SQL_ROUTE_ENABLED and is_coverage_query(body.query):
                _code = extract_code(body.query)
                _ranges = CoverageRepository(db).get_ranges() if _code else {}
                if _code and _ranges:
                    _verdict = judge_coverage(_code, _ranges, extract_coverage(body.query))
                    # reconcile payout은 담보 키워드로 전 상품에서 매칭(document_id는 payout 상품 아님)
                    _canswer = _coverage_answer_reconciled(db, None, _code, _verdict)
                    rec.route = {**(rec.route or {}), "strategy": "sql"}
                    api_logger.info(f"SQL 경로(coverage) 적중: {_code} → {_verdict['verdict']}")
                    return {
                        "trace_id": rec.trace_id,
                        "query": body.query,
                        "answer": _canswer,
                        "elapsed_ms": int((time.time() - t0) * 1000),
                        "sources": [],
                        "route": {"strategy": "sql", "query_type": route.query_type.value},
                        "citations": [{"claim": _canswer, "refs": [_code], "supported_by_chunks": []}],
                    }

            # SQL 경로 — 면책 상세("뭐가 면책?"). coverage(코드 판정) 뒤 — "보장 안 되는 경우?"는
            # coverage 게이트가 켜지나 코드 없어 여기로 떨어진다. 상품은 담보 키워드로 해소.
            if SQL_ROUTE_ENABLED and is_exclusion_query(body.query):
                _ep = ProductRepository(db).get_terms(coverage_kw=coverage_hint(body.query))
                _excls = PayoutRepository(db).get_exclusions(_ep["product_id"]) if _ep else []
                if _excls:
                    _eanswer = format_exclusions(_excls, _ep.get("resolution_note"))
                    rec.route = {**(rec.route or {}), "strategy": "sql"}
                    api_logger.info(f"SQL 경로(exclusion) 적중: {_ep['product_id']}")
                    return {
                        "trace_id": rec.trace_id,
                        "query": body.query,
                        "answer": _eanswer,
                        "elapsed_ms": int((time.time() - t0) * 1000),
                        "sources": [],
                        "route": {"strategy": "sql", "query_type": route.query_type.value},
                        "citations": [{"claim": _eanswer,
                                       "refs": [str(e["jo"]) for e in _excls if e.get("jo")],
                                       "supported_by_chunks": []}],
                    }

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
            # heading_path를 content 앞에 포함 — 청크는 항 단위로 쪼개져 조 번호("제10조")가 본문이 아닌
            # heading_path에만 있음. 이걸 빼면 verifier가 답변의 "제10조" 인용을 context에 없다고 오판(hard_fail).
            from .rag.grader import Chunk as VerifyChunk
            verify_chunks = [
                VerifyChunk(
                    id=str(r[0].id),
                    content=(f"{(r[0].payload or {}).get('heading_path', '')}\n"
                             f"{(r[0].payload or {}).get('content', '')}").strip(),
                )
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


@router.post("/payout", summary="결정론 지급 질의 (SQL 경로)", tags=["Retrieval"])
def payout(body: PayoutRequest, db: Session = Depends(get_db)):
    """3경로 라우터의 SQL 경로 — "얼마/언제" 질의를 payout_rule에서 **결정론**으로 집어온다.

    RAG(/answer)와 분리된 사이드카: `matched=false`면 RAG로 폴백하라는 신호(precision-first —
    담보 미검출·규칙 미매칭이면 억지 지급률 대신 RAG). 서빙 로직은 `rag/payout_sql.py`,
    데이터는 `PayoutRepository`(payout_rule). 라우터 통합(질의 유형 감지 후 SQL/RAG 분기)은 후속.
    """
    _apply_input_guard(body)
    repo = PayoutRepository(db)
    rule = select_payout(repo.get_rules(body.product_id), body.query)
    # 면책 강제첨부 — 지급률만 답하고 면책 빠뜨리면 소비자 손해(완결성).
    exclusions = repo.get_exclusions(rule["product_id"], rule.get("coverage")) if rule else []
    return {
        "query": body.query,
        "route": "sql",
        "matched": rule is not None,
        "answer": format_payout_complete(rule, exclusions),
        "rule": rule,           # 근거: 매칭된 payout_rule row. miss면 null
        "exclusions": exclusions,  # 강제첨부된 면책 조 [{jo, title}]
    }


@router.post("/terms", summary="결정론 계약조건 질의 (SQL 경로)", tags=["Retrieval"])
def terms(body: TermsRequest, db: Session = Depends(get_db)):
    """3경로 라우터의 SQL 경로 — "언제까지?"(청약철회·갱신)를 product에서 결정론으로.

    **준용 NULL 철학**: 특약은 청약철회 NULL이 정답(보통약관 준용 소관) — 억지 값 대신
    "주계약 준용 소관, 확인 필요". 상품 미해소면 matched=false→RAG. 로직 `rag/terms_sql.py`.
    """
    _apply_input_guard(body)
    product = ProductRepository(db).get_terms(body.product_id, coverage_hint(body.query))
    return {
        "query": body.query,
        "route": "sql",
        "matched": product is not None,
        "answer": format_terms(product),
        "product": product,   # 근거 (is_renewable·cooling_off_days·resolution_note). miss면 null
    }


@router.post("/coverage", summary="결정론 보장판정 (별표3 ICD, SQL 경로)", tags=["Retrieval"])
def coverage(body: CoverageRequest, db: Session = Depends(get_db)):
    """3경로 라우터의 SQL 경로 — "이 병(코드) 보장돼요?"를 별표3 ICD 범위로 3-값 판정
    (보장/미보장→리다이렉트/판정불가). 억지 판정 안 함(판정불가=precision-first). 병명→코드는
    별도 계층 — 코드 미특정이면 matched=false→RAG. 로직 `rag/coverage_sql.py`.
    """
    _apply_input_guard(body)
    code = extract_code(body.query)
    ranges = CoverageRepository(db).get_ranges(body.product_id)
    verdict = judge_coverage(code, ranges, extract_coverage(body.query)) if code and ranges else None
    return {
        "query": body.query,
        "route": "sql",
        "matched": verdict is not None,   # 코드 특정 + 판정 근거 존재 (판정불가도 결정론 답)
        "answer": _coverage_answer_reconciled(db, body.product_id, code, verdict),  # 판정 + payout 정합
        "code": code,
        "verdict": verdict,   # {verdict, coverage, redirect_coverage, evidence}. 근거
    }


@router.post("/exclusion", summary="결정론 면책 상세 (SQL 경로)", tags=["Retrieval"])
def exclusion(body: ExclusionRequest, db: Session = Depends(get_db)):
    """3경로 라우터의 SQL 경로 — "뭐가 면책이야?"를 면책 조 사유로 결정론 나열
    (고의·전쟁내란 등). payout의 강제첨부와 달리 면책만 묻는 단독 질의. 상품 미해소면
    matched=false→RAG. 로직 `rag/exclusion_sql.py`.
    """
    _apply_input_guard(body)
    product = ProductRepository(db).get_terms(body.product_id, coverage_hint(body.query))
    pid = product["product_id"] if product else None
    exclusions = PayoutRepository(db).get_exclusions(pid) if pid else []
    resolution = product.get("resolution_note") if product else None
    return {
        "query": body.query,
        "route": "sql",
        "matched": bool(exclusions) or bool(resolution and "준용" in resolution),
        "answer": format_exclusions(exclusions, resolution),   # 사유 + 준용 완결성
        "exclusions": exclusions,   # 근거 면책 조 [{jo, title, body}]
    }


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
