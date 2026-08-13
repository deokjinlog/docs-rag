"""SQL 경로 end-to-end 통합 — 실 payout_rule(DB) → PayoutRepository → select_payout.

test_payout_sql.py(픽스처 순수 로직)와 달리 **실 적재된 payout_rule**을 SELECT해 결정론
답변 골든을 채점한다. B5 사이드카의 serving 컴포넌트(repository DB SELECT)를 커버.

실행: docker compose exec api uv run pytest tests/rag/test_payout_sql_integration.py -m integration
(host에서는 integration 마크로 자동 skip — 컨테이너 DB 세션 필요.)
"""

import pytest


# payout QA 골든 (golden_payout_qa.jsonl과 동일 — 실 DB 값으로 재현)
_GOLDEN = [
    ("중환자실 입원하면 하루 얼마 받아요?", 1),
    ("레진 충전 질병으로 1년 지나서 받으면 얼마?", 50),
    ("레진 충전 질병 90일 안에 받으면 얼마?", 0),
    ("암진단자금 15세 이상이 90일 이내 진단이면 얼마?", 0),
    ("12개월 소득보장 수술 받으면 매월 얼마?", 30),
]


@pytest.mark.integration
def test_payout_repository_real_db_golden_5_of_5():
    """실 payout_rule → PayoutRepository.get_rules() → select_payout 골든 5/5 end-to-end.

    payout_rule 미적재면 skip(적재: `load_payout.py --load`).
    """
    from v1.config import task_session
    from v1.repository import PayoutRepository
    from v1.rag.payout_sql import select_payout

    with task_session() as db:
        rows = PayoutRepository(db).get_rules()

    if len(rows) < 5:
        pytest.skip(f"payout_rule 미적재({len(rows)}행) — load_payout.py --load 필요")

    for query, expect in _GOLDEN:
        r = select_payout(rows, query)
        assert r is not None, f"미스: {query}"
        assert r["rate_pct"] == expect, f"{query} → {r['rate_pct']} (기대 {expect})"


@pytest.mark.integration
def test_payout_repository_rate_pct_is_int():
    """rate_pct는 int로 정규화돼야(Decimal→int) — 골든 정수 비교·JSON 직렬화 일관."""
    from v1.config import task_session
    from v1.repository import PayoutRepository

    with task_session() as db:
        rows = PayoutRepository(db).get_rules("LINA_ICU_2024")

    if not rows:
        pytest.skip("LINA_ICU_2024 payout_rule 미적재")
    for r in rows:
        if r.get("rate_pct") is not None:
            assert isinstance(r["rate_pct"], int), f"rate_pct 타입 {type(r['rate_pct'])}"


# ── POST /payout 엔드포인트 (SQL 경로 사이드카) ─────────────────────────────
@pytest.mark.integration
def test_payout_endpoint_deterministic_hit():
    """지급 질의 → matched=True + 결정론 answer + 근거 rule."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    resp = client.post("/api/v1/docs-rag/payout", json={
        "query": "중환자실 입원하면 하루 얼마 받아요?", "service_code": "01",
    })
    assert resp.status_code == 200
    d = resp.json()
    if not d["matched"]:
        pytest.skip("payout_rule 미적재 — load_payout.py --load 필요")
    assert d["route"] == "sql"
    assert d["rule"]["rate_pct"] == 1 and "1%" in d["answer"]
    # 완결성: 면책 강제첨부 (지급률만 답하면 안 됨)
    assert "면책" in d["answer"] and d.get("exclusions")


@pytest.mark.integration
def test_payout_endpoint_rag_fallback_on_non_payout():
    """비-payout 질의(청약철회) → matched=False + '→RAG' 폴백 신호(precision-first)."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    resp = client.post("/api/v1/docs-rag/payout", json={
        "query": "청약철회는 언제까지 가능한가요?", "service_code": "01",
    })
    assert resp.status_code == 200
    d = resp.json()
    assert d["matched"] is False and "→RAG" in d["answer"] and d["rule"] is None


# ── /answer 자동 SQL 라우팅 (B5) — amount 질의는 SQL, 해석은 RAG ────────────
def _mock_ranked_chunk(content: str):
    from unittest.mock import MagicMock
    point = MagicMock()
    point.id = "c1"; point.score = 0.5
    point.payload = {"content": content, "page_range": [1, 1], "chunk_type": "text",
                     "heading_path": "", "document_id": "d", "part_index": 1,
                     "part_total": 1, "service_code": "01"}
    return (point, 0.85)


@pytest.mark.integration
def test_answer_routes_amount_query_to_sql_no_llm():
    """/answer amount 질의 → route=sql + 결정론 답, invoke_clean(LLM) 미호출."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        resp = client.post("/api/v1/docs-rag/answer", json={
            "query": "중환자실 입원하면 하루 얼마 받아요?", "service_code": "01",
        })
    assert resp.status_code == 200
    d = resp.json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("payout_rule 미적재 — SQL 라우팅 안 됨")
    assert "1%" in d["answer"] and mock_invoke.call_count == 0  # 결정론 = LLM 미호출
    assert "면책" in d["answer"]  # 완결성: SQL 답에도 면책 강제첨부


@pytest.mark.integration
def test_answer_interpretation_query_not_hijacked_by_sql():
    """담보를 언급해도 '언제 지급(지급사유)'은 SQL로 안 새고 RAG로 — route!=sql, LLM 호출."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean", return_value="제5조에 따라 지급한다.") as mock_invoke, \
         patch("v1.router.search_and_rerank", return_value=[_mock_ranked_chunk("제5조 지급사유")]), \
         patch("v1.router.expand_siblings", return_value="제5조 지급사유 context"):
        client = TestClient(app)
        resp = client.post("/api/v1/docs-rag/answer", json={
            "query": "중환자실 입원급여금은 언제 지급되나요?", "service_code": "01",
        })
    assert resp.status_code == 200
    d = resp.json()
    assert d["route"]["strategy"] != "sql", "해석 질의가 SQL로 오라우팅됨"
    assert mock_invoke.call_count >= 1, "RAG(LLM)로 안 감"
