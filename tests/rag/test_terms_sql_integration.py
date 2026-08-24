"""terms SQL 경로 end-to-end 통합 — 실 product(DB) → ProductRepository → format_terms.

준용 NULL 철학(특약 청약철회="주계약 준용 소관")과 복합약관 실값(15일)을 실 DB로 검증.
실행: docker compose exec api uv run pytest tests/rag/test_terms_sql_integration.py -m integration
"""

import pytest


@pytest.mark.integration
def test_terms_repository_junyong_null_for_tukyak():
    """특약(LINA_ICU)은 청약철회 NULL + resolution_note(준용 소관)."""
    from v1.config import task_session
    from v1.repository import ProductRepository

    with task_session() as db:
        p = ProductRepository(db).get_terms("LINA_ICU_2024")
    if not p:
        pytest.skip("product 미적재")
    assert p["cooling_off_days"] is None and p.get("resolution_note")   # 준용 NULL


@pytest.mark.integration
def test_terms_endpoint_junyong_vs_value():
    """/terms — 특약은 '준용 소관', 복합약관은 실값."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    r1 = client.post("/api/v1/docs-rag/terms", json={
        "query": "중환자실 특약 청약철회 언제까지?", "service_code": "01"}).json()
    r2 = client.post("/api/v1/docs-rag/terms", json={
        "query": "New치아보험 청약철회 며칠 이내?", "service_code": "01"}).json()
    if not (r1["matched"] and r2["matched"]):
        pytest.skip("product terms 미적재 — load_terms.py --load 필요")
    assert "준용" in r1["answer"] and "확인 필요" in r1["answer"]   # 특약 = 준용 NULL
    assert "15일 이내" in r2["answer"]                              # 복합약관 = 실값


@pytest.mark.integration
def test_terms_endpoint_no_product_rag_fallback():
    """상품 특정 불가(담보 없음) → matched=false → RAG 폴백."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    d = client.post("/api/v1/docs-rag/terms", json={
        "query": "청약철회는 언제까지 가능한가요?", "service_code": "01"}).json()
    assert d["matched"] is False and "→RAG" in d["answer"]


@pytest.mark.integration
def test_terms_endpoint_kb_brand_resolves_to_base():
    """KB 상품명 브랜드로 base 상품 해소 → 청약철회 15일 실값(복합 KB 보통약관)."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    r = client.post("/api/v1/docs-rag/terms", json={
        "query": "KB 골든라이프 청약철회 언제까지 가능한가요?", "service_code": "01"}).json()
    if not r["matched"]:
        pytest.skip("KB terms 미적재 — load_terms.py --load 필요")
    assert r["product"]["product_id"] == "KB_GOLDENLIFE_2026"   # 정확한 KB base 해소
    assert "15일 이내" in r["answer"] and "갱신형" in r["answer"]


@pytest.mark.integration
def test_answer_routes_kb_terms_to_sql_no_llm():
    """/answer KB 계약조건 질의 → route=sql, 15일, LLM 미호출(교차회사 오해소 없음)."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        d = client.post("/api/v1/docs-rag/answer", json={
            "query": "슬기로운 간편실속 청약철회 며칠 이내인가요?", "service_code": "01"}).json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("KB terms 미적재 — 라우팅 안 됨")
    assert "15일 이내" in d["answer"] and mock_invoke.call_count == 0
    assert d["citations"][0]["refs"] == ["KB_SEULGI_2023"]   # 슬기로운 → 정확 해소


@pytest.mark.integration
def test_answer_routes_terms_query_to_sql_no_llm():
    """/answer 계약조건 질의(갱신) → route=sql, LLM 미호출."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        d = client.post("/api/v1/docs-rag/answer", json={
            "query": "중환자실 특약 갱신되나요?", "service_code": "01"}).json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("product 미적재 — terms 라우팅 안 됨")
    assert "갱신" in d["answer"] and mock_invoke.call_count == 0
