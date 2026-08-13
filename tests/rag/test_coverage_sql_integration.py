"""보장판정 SQL 경로 end-to-end — 실 coverage_range(DB) → CoverageRepository → judge_coverage.

golden_coverage(7/7)를 실 DB 코드범위로 재현 + /coverage 엔드포인트 + /answer 라우팅.
실행: docker compose exec api uv run pytest tests/rag/test_coverage_sql_integration.py -m integration
"""

import json
import os

import pytest


@pytest.mark.integration
def test_coverage_repository_golden_7_of_7():
    """실 coverage_range → CoverageRepository.get_ranges → judge_coverage 골든 7/7."""
    from v1.config import task_session
    from v1.repository import CoverageRepository
    from v1.rag.coverage_sql import judge_coverage

    with task_session() as db:
        ranges = CoverageRepository(db).get_ranges("DIRECT_INPT_2024")
    if not ranges:
        pytest.skip("coverage_range 미적재 — load_coverage.py --load 필요")

    golden_path = os.path.join(os.path.dirname(__file__), "..", "..",
                               "data", "eval", "golden_coverage.jsonl")
    gold = [json.loads(l) for l in open(golden_path, encoding="utf-8") if l.strip()]
    for g in gold:
        v = judge_coverage(g["code"], ranges, g["coverage"])
        assert v["verdict"] == g["expected"], f"{g['code']}/{g['coverage']} → {v['verdict']} (기대 {g['expected']})"


@pytest.mark.integration
def test_coverage_endpoint_3valued():
    """/coverage — 보장/미보장(리다이렉트)/판정불가."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    def _ans(q):
        return client.post("/api/v1/docs-rag/coverage",
                           json={"query": q, "service_code": "01"}).json()

    covered = _ans("C50은 보장되나요?")
    if not covered["matched"]:
        pytest.skip("coverage_range 미적재")
    assert covered["verdict"]["verdict"] == "보장"
    uncov = _ans("D05는 암진단자금으로 보장돼요?")
    assert uncov["verdict"]["verdict"] == "미보장" and uncov["verdict"]["redirect_coverage"]
    undet = _ans("Z99는 보장되나요?")
    assert undet["verdict"]["verdict"] == "판정불가"


@pytest.mark.integration
def test_coverage_endpoint_no_code_rag_fallback():
    """병명→코드 못 짚으면 matched=false → RAG."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)
    d = client.post("/api/v1/docs-rag/coverage",
                    json={"query": "두통은 보장되나요?", "service_code": "01"}).json()
    assert d["matched"] is False and "→RAG" in d["answer"]


@pytest.mark.integration
def test_answer_routes_coverage_query_to_sql_no_llm():
    """/answer 보장판정 질의 → route=sql, LLM 미호출, 리다이렉트 노출."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from api import app

    with patch("v1.router.invoke_clean") as mock_invoke:
        client = TestClient(app)
        d = client.post("/api/v1/docs-rag/answer", json={
            "query": "D05는 암진단자금으로 보장되나요?", "service_code": "01"}).json()
    if d["route"]["strategy"] != "sql":
        pytest.skip("coverage_range 미적재")
    assert "미보장" in d["answer"] and mock_invoke.call_count == 0
