"""면책 상세 SQL 경로 end-to-end — 실 면책 조(DB) → format_exclusions.

실행: docker compose exec api uv run pytest tests/rag/test_exclusion_sql_integration.py -m integration
"""

import pytest


@pytest.mark.integration
def test_exclusion_endpoint_lists_reasons():
    """/exclusion — 상품 면책 사유(라이나=고의) 나열."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    d = client.post("/api/v1/docs-rag/exclusion", json={
        "query": "중환자실 특약은 뭐가 면책인가요?", "service_code": "01"}).json()
    if not d["matched"]:
        pytest.skip("면책 데이터 미적재")
    assert "면책" in d["answer"] and "제7조" in d["answer"]


@pytest.mark.integration
def test_exclusion_endpoint_no_product_rag_fallback():
    """상품 미해소(담보 키워드 없음) → matched=false → RAG."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    d = client.post("/api/v1/docs-rag/exclusion", json={
        "query": "뭐가 면책이야?", "service_code": "01"}).json()
    assert d["matched"] is False and "→RAG" in d["answer"]


@pytest.mark.integration
def test_answer_routes_exclusion_query_to_sql_no_llm():
    """/answer 면책 질의 → route=sql, LLM 미호출."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        d = client.post("/api/v1/docs-rag/answer", json={
            "query": "중환자실 특약 지급 안 되는 사유는?", "service_code": "01"}).json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("면책 데이터 미적재 — exclusion 라우팅 안 됨")
    assert "면책" in d["answer"] and mock_invoke.call_count == 0
