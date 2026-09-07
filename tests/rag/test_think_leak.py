"""reasoning 모델의 <think> 블록 누출 방어 — 실측 사고 기반 회귀 테스트.

2026-09-07 실측: Qwen3 가 CRAG 재작성 중 max_tokens 에 걸려 `</think>` 없이 잘렸다.
기존 정규식 `<think>.*?</think>` 는 **닫힌 블록만** 매칭하므로 추론 전문 5,000자가
그대로 검색 쿼리가 됐다(query_embed 200ms → 8,181ms). 사용자 답변 경로에서 같은 일이
나면 내부 추론이 그대로 노출된다.
"""
import re

import pytest

from src.v1.rag.clients import THINK_RE, THINK_UNCLOSED_RE
from src.v1.rag.search import REWRITE_MAX_CHARS, rewrite_query


def _clean(raw: str) -> str:
    """invoke_clean 의 순수 부분(LLM 호출 제외)."""
    return THINK_UNCLOSED_RE.sub("", THINK_RE.sub("", raw)).strip()


def test_닫힌_think_블록은_제거된다():
    assert _clean("<think>내부 추론</think>실제 답변") == "실제 답변"


def test_닫히지_않은_think_블록도_제거된다():
    """토큰 한도에 걸려 </think> 가 안 나온 경우 — 이게 실제로 터진 케이스."""
    raw = "<think>Okay, let's tackle this query. " + ("blah " * 500)
    assert _clean(raw) == ""


def test_추론_뒤_본문이_잘린_경우_앞부분은_보존():
    raw = "요약: 지급됩니다.\n<think>왜냐하면 제5조에 의하면..."
    assert _clean(raw) == "요약: 지급됩니다."


def test_think_없는_평범한_응답은_그대로():
    assert _clean("  중환자실 정의  ") == "중환자실 정의"


def test_여러_닫힌_블록도_전부_제거():
    assert _clean("<think>a</think>답<think>b</think>변") == "답변"


class _FakeInvoke:
    def __init__(self, ret): self.ret = ret
    def __call__(self, _messages): return self.ret


@pytest.mark.parametrize("bad,이유", [
    ("", "빈 문자열 — 추론만 하다 잘린 경우"),
    ("x" * (REWRITE_MAX_CHARS + 1), "길이 초과 — 쿼리가 아니라 사설"),
])
def test_수상한_재작성은_원_질의로_폴백(monkeypatch, bad, 이유):
    """나쁜 재작성은 재작성 안 한 것보다 나쁘다 — CRAG 는 실패하면 거절로 가므로 손해 없음."""
    monkeypatch.setattr("src.v1.rag.search.invoke_clean", _FakeInvoke(bad))
    original = "해지된 특약을 다시 살릴 수 있나요?"
    assert rewrite_query(original) == original, 이유


def test_정상_재작성은_그대로_쓴다(monkeypatch):
    monkeypatch.setattr("src.v1.rag.search.invoke_clean", _FakeInvoke("특약 부활 효력회복 절차"))
    assert rewrite_query("해지된 특약 다시 살리기") == "특약 부활 효력회복 절차"
