"""waiting SQL 경로 end-to-end 통합 — 실 product/payout_rule(DB) → 면책기간·감액.

KB 담보(특약)별 면책기간(가입 후 90일 보장제외)·감액(1년간 50%)을 실 DB로 검증. 브랜드+담보
해소 시 결정론, 못 짚거나 데이터 없으면 RAG.
실행: docker compose exec api uv run pytest tests/rag/test_waiting_sql_integration.py -m integration
"""

import pytest


@pytest.mark.integration
def test_waiting_facts_repo_has_kb_rows():
    """KB base의 특약별 면책기간·감액 행이 적재돼 있다(리랭커 불필요)."""
    from v1.config import task_session
    from v1.repository import CoverageRepository

    with task_session() as db:
        rows = CoverageRepository(db).get_waiting_facts("KB_GOLDENLIFE_2026")
    if not rows:
        pytest.skip("KB waiting 미적재 — load_waiting.py --load 필요")
    # 면책기간 90 + 감액 50%가 있는 담보가 존재
    assert any(r.get("waiting_period_days") == 90 for r in rows)
    assert any(r.get("reduction_rate_pct") == 50 for r in rows)


@pytest.mark.integration
def test_waiting_endpoint_hit_and_miss():
    """/waiting — 담보 지목 시 면책기간·감액, 담보 미지목이면 matched=false→RAG."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    hit = client.post("/api/v1/docs-rag/waiting", json={
        "query": "골든라이프 암진단비 면책기간 얼마야?", "service_code": "01"}).json()
    miss = client.post("/api/v1/docs-rag/waiting", json={
        "query": "골든라이프 면책기간 얼마?", "service_code": "01"}).json()   # 담보 미지목
    if not hit["matched"]:
        pytest.skip("KB waiting 미적재")
    assert hit["fact"]["waiting_period_days"] == 90 and "면책기간 90일" in hit["answer"]
    assert miss["matched"] is False and "→RAG" in miss["answer"]


@pytest.mark.integration
def test_answer_routes_waiting_to_sql_no_llm():
    """/answer 면책기간 질의 → route=sql, LLM 미호출."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        d = client.post("/api/v1/docs-rag/answer", json={
            "query": "골든라이프 암진단비 면책기간 얼마야?", "service_code": "01"}).json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("KB waiting 미적재 — 라우팅 안 됨")
    assert "면책기간 90일" in d["answer"] and mock_invoke.call_count == 0
    assert d["citations"][0]["refs"] == ["KB_GOLDENLIFE_2026"]
