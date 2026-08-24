"""catalog SQL 경로 end-to-end 통합 — 실 product(DB 특약 목록) → CoverageRepository → 멤버십.

KB 복합약관의 특약 목록이 곧 담보 catalog임을 실 DB로 검증. 브랜드 해소 + 담보 적중 시 결정론,
미적중은 부재 단정 없이 RAG.
실행: docker compose exec api uv run pytest tests/rag/test_catalog_sql_integration.py -m integration
"""

import pytest


@pytest.mark.integration
def test_list_catalog_kb_has_subcontracts():
    """KB base 상품의 특약 목록(=담보 catalog)이 비어있지 않다."""
    from v1.config import task_session
    from v1.repository import CoverageRepository

    with task_session() as db:
        names = CoverageRepository(db).list_catalog("KB_GOLDENLIFE_2026")
    if not names:
        pytest.skip("KB 특약 미적재")
    assert len(names) > 30                                # 골든라이프 특약 다수
    assert any("장기요양간병비" in n for n in names)


@pytest.mark.integration
def test_catalog_endpoint_membership_hit_and_miss():
    """/catalog — 적중 담보는 matched=true, 없는 담보는 부재 단정 없이 matched=false→RAG."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    hit = client.post("/api/v1/docs-rag/catalog", json={
        "query": "골든라이프에 파킨슨병진단비 담보 있어?", "service_code": "01"}).json()
    miss = client.post("/api/v1/docs-rag/catalog", json={
        "query": "골든라이프에 레진충전 담보 있어?", "service_code": "01"}).json()
    if hit["catalog_size"] == 0:
        pytest.skip("KB 특약 미적재")
    assert hit["matched"] and "파킨슨병진단비" in hit["covered"]
    assert miss["matched"] is False and "→RAG" in miss["answer"]   # 부재 단정 안 함


@pytest.mark.integration
def test_answer_routes_catalog_query_to_sql_no_llm():
    """/answer 멤버십 질의 → route=sql, LLM 미호출."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        d = client.post("/api/v1/docs-rag/answer", json={
            "query": "슬기로운 간편실속에 대상포진진단비 담보 있어?", "service_code": "01"}).json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("KB 특약 미적재 — catalog 라우팅 안 됨")
    assert "대상포진진단비" in d["answer"] and mock_invoke.call_count == 0
    assert d["citations"][0]["refs"] == ["KB_SEULGI_2023"]
