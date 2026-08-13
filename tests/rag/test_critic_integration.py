"""Critic dispatch end-to-end (FastAPI TestClient + mocked LLM/Qdrant).

router.py에 wire된 critic 분기가 실제 endpoint 호출에서 정확히 발동하는지 검증.
classify_failure / build_hint 단위 테스트와 보완 관계 — 단위 테스트가 잡지 못하는
"router의 인자 전달 / 재검증 흐름 / 응답 projection / trace.critic 기록" 통합을 잡는다.

실행 환경:
  - docker 컨테이너 안에서만 실행 가능 (router.py module-level import가 /app/model 경로 의존).
  - host에서는 reranker 모델 파일 부재로 import 단계에서 FileNotFoundError 발생.
  - 실행: docker compose exec api uv run pytest tests/rag/test_critic_integration.py -v -m integration

기본 pytest run (uv run pytest tests/rag/) 에서는 자동 skip되어 단위 테스트 흐름 방해 없음.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.integration


def _mock_ranked_chunk(chunk_id: str, content: str, score: float = 0.85):
    """ranked: list[(point, rerank_score)] 형태 모방 — format_sources / expand_siblings 호환."""
    point = MagicMock()
    point.id = chunk_id
    point.score = 0.5  # rrf_score
    point.payload = {
        "content": content,
        "page_range": [1, 1],
        "chunk_type": "text",
        "heading_path": "",
        "document_id": "test_doc",
        "part_index": 1,
        "part_total": 1,
        "service_code": "01",
    }
    return (point, score)


# 모든 LLM 호출은 invoke_clean() 단일 진입점 경유 → router.py 의 그 import binding을 patch.
# 옛 패턴 patch("...llm")이 아닌 이유: invoke_clean 안에서 clients.llm 호출하므로
# router 모듈에 바인딩된 llm 을 patch해도 안 잡힘 (lookup이 clients 모듈에서 일어남).
#
# patch 경로가 `v1.router.X` (`src.v1.router.X` 가 아님): pyproject.toml `pythonpath=["src"]`
# 로 src/가 sys.path 루트 → 런타임과 동일하게 v1.router 가 top-level module 이름.
# api.py 가 import한 `v1.router` 와 같은 module instance여야 mock이 적중함.


# Critic은 기본 꺼짐(CRITIC_DISPATCH_ENABLED=false, design-retrospective §1.5) — 이 테스트들은
# critic '배선'(켰을 때 정확히 발동/분기하는지)을 검증하므로 flag를 켠다.
@patch("v1.router.CRITIC_DISPATCH_ENABLED", True)
@patch("v1.router.expand_siblings")
@patch("v1.router.search_and_rerank")
@patch("v1.router.invoke_clean")
def test_answer_endpoint_triggers_regenerate_on_generation_error(
    mock_invoke, mock_search, mock_expand,
):
    """첫 답변이 context의 인접 조항을 잘못 인용 → critic이 generation_error 분류 → regenerate."""
    from fastapi.testclient import TestClient
    from api import app

    # context: 제42조만 존재. 첫 답변은 제43조 잘못 인용 (generation_error 케이스).
    context_content = "제42조(보험금 지급 사유) 피보험자는 제42조 제1항에 따라 청구한다."
    mock_search.return_value = [_mock_ranked_chunk("c1", context_content)]
    mock_expand.return_value = context_content

    # invoke_clean 호출 시퀀스: 1차 답변 (제43조 환각) → 2차 답변 (재생성, 제42조 정정).
    # invoke_clean 은 이미 think 태그 strip 후 string 반환이므로 raw string side_effect.
    mock_invoke.side_effect = [
        "제43조에 따라 보험금을 지급한다.",
        "제42조에 따라 보험금을 지급한다.",
    ]

    client = TestClient(app)
    resp = client.post("/api/v1/docs-rag/answer", json={
        "query": "보험금 지급 조항은?",
        "service_code": "01",
        "top_k": 3,
    })
    assert resp.status_code == 200
    data = resp.json()

    # invoke_clean이 2회 호출됐어야 함 (초기 + critic regenerate)
    assert mock_invoke.call_count == 2, (
        f"regenerate 미발동: invoke_clean 호출 {mock_invoke.call_count}회 (기대 2회)"
    )
    assert "제42조" in data["answer"]
    if "verification" in data:
        assert data["verification"].get("escalation_required") is not True


@patch("v1.router.CRITIC_DISPATCH_ENABLED", True)
@patch("v1.router.expand_siblings")
@patch("v1.router.search_and_rerank")
@patch("v1.router.invoke_clean")
def test_answer_endpoint_escalates_on_retrieval_gap(
    mock_invoke, mock_search, mock_expand,
):
    """답변이 context에 전혀 없는 조항(제99조) 인용 → retrieval_gap → regenerate 금지 + escalation flag."""
    from fastapi.testclient import TestClient
    from api import app

    # context: 제1·2·5조만. 제99조는 인접도 안 됨.
    context_content = "제1조 일반사항 ... 제2조 정의 ... 제5조 적용범위 ..."
    mock_search.return_value = [_mock_ranked_chunk("c1", context_content)]
    mock_expand.return_value = context_content

    mock_invoke.side_effect = ["제99조에 따라 보장된다."]

    client = TestClient(app)
    resp = client.post("/api/v1/docs-rag/answer", json={
        "query": "보장 조항은?",
        "service_code": "01",
        "top_k": 3,
    })
    assert resp.status_code == 200
    data = resp.json()

    # invoke_clean 1회만 (regenerate 금지)
    assert mock_invoke.call_count == 1, (
        f"retrieval_gap에서 잘못 regenerate 발동: invoke_clean 호출 {mock_invoke.call_count}회"
    )
    assert "verification" in data
    assert data["verification"].get("escalation_required") is True


@patch("v1.router.expand_siblings")
@patch("v1.router.search_and_rerank")
@patch("v1.router.invoke_clean")
def test_answer_endpoint_no_critic_invocation_on_pass(
    mock_invoke, mock_search, mock_expand,
):
    """답변이 context와 일치 → verification pass → critic 미개입, 응답에 escalation 없음."""
    from fastapi.testclient import TestClient
    from api import app

    context_content = "제1조에 따라 지급한다. 한도는 1,000만원이다."
    mock_search.return_value = [_mock_ranked_chunk("c1", context_content)]
    mock_expand.return_value = context_content

    mock_invoke.side_effect = ["제1조에 따라 1,000만원을 한도로 지급한다."]

    client = TestClient(app)
    resp = client.post("/api/v1/docs-rag/answer", json={
        "query": "지급 조건은?",
        "service_code": "01",
        "top_k": 3,
    })
    assert resp.status_code == 200
    data = resp.json()

    assert mock_invoke.call_count == 1
    if "verification" in data:
        assert data["verification"].get("escalation_required") is not True
