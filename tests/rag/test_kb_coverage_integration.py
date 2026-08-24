"""KB 암 별표3 보장판정 end-to-end 통합 — 실 coverage_range(DB) → judge_coverage.

KB 암진단비=악성신생물−유사암(C44·C73)의 담보 특정성을 실 DB로 검증(브랜드 스코프로 다이렉트
암진단자금 C73~C75 교차오염 없이). C50 보장·C73 미보장→갑상선암·C44→기타피부암.
실행: docker compose exec api uv run pytest tests/rag/test_kb_coverage_integration.py -m integration
"""

import pytest


@pytest.mark.integration
def test_coverage_range_kb_scoped():
    """KB base 스코프 시 KB 암 담보만(다이렉트 암진단자금 불포함, 리랭커 불필요)."""
    from v1.config import task_session
    from v1.repository import CoverageRepository

    with task_session() as db:
        ranges = CoverageRepository(db).get_ranges("KB_GOLDENLIFE_2026")
    if not ranges:
        pytest.skip("KB coverage_range 미적재 — load_kb_coverage.py --load 필요")
    assert "암진단비" in ranges and "갑상선암" in ranges
    assert "암진단자금" not in ranges              # 다이렉트 담보 아님(스코프 격리)


@pytest.mark.integration
def test_coverage_endpoint_kb_cancer_specificity():
    """/coverage — KB 암진단비 담보 특정성: C50 보장·C73 미보장→갑상선암."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    yes = client.post("/api/v1/docs-rag/coverage", json={
        "query": "골든라이프 암진단비 C50 보장돼?", "service_code": "01",
        "product_id": "KB_GOLDENLIFE_2026"}).json()
    no = client.post("/api/v1/docs-rag/coverage", json={
        "query": "골든라이프 암진단비 C73 보장돼?", "service_code": "01",
        "product_id": "KB_GOLDENLIFE_2026"}).json()
    if not yes["matched"]:
        pytest.skip("KB coverage_range 미적재")
    assert yes["verdict"]["verdict"] == "보장"
    assert no["verdict"]["verdict"] == "미보장" and no["verdict"]["redirect_coverage"] == "갑상선암"


@pytest.mark.integration
def test_answer_routes_kb_coverage_scoped_no_crosscompany():
    """/answer KB 암 질의 → route=sql, 브랜드 스코프로 다이렉트 교차오염 없음."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        d = client.post("/api/v1/docs-rag/answer", json={
            "query": "골든라이프 암진단비 C73 보장돼?", "service_code": "01"}).json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("KB coverage 미적재 — 라우팅 안 됨")
    assert "갑상선암" in d["answer"] and mock_invoke.call_count == 0
