"""시맨틱 라우터 로직 단위 테스트 — 임베딩 모델 없이 돈다.

`route_semantic(encode=...)` 로 가짜 인코더를 주입해 **임계·기권 로직**만 검증한다.
실제 임베딩 품질(정확도)은 골든 채점기 `scripts/eval_semantic_route.py` 소관 —
여기서 모델을 로드하면 단위 테스트가 2GB 모델을 끌고 오게 되어 CLAUDE.md의
"unit test 가 의도치 않게 모델 로드 트리거하는 회귀 방지" 규율을 깬다.
"""
import pytest

from src.v1.config.settings import SEMANTIC_ROUTE_MIN_MARGIN, SEMANTIC_ROUTE_MIN_SCORE
from src.v1.rag import semantic_router as sr


@pytest.fixture(autouse=True)
def _clean_cache():
    sr.reset_cache()
    yield
    sr.reset_cache()


def _fake_encode(scores: dict[str, float]):
    """예시 텍스트 → 지정한 유사도가 나오도록 하는 가짜 인코더.

    질의 벡터를 [1, 0, 0, ...] 로, 각 예시 벡터의 첫 성분을 그 경로의 목표 점수로 두면
    내적 = 목표 점수가 된다. 경로 순서는 ROUTE_EXAMPLES 키 순서를 따른다.
    """
    def encode(texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            if t == "__QUERY__":
                out.append([1.0, 0.0])
                continue
            route = next(r for r, exs in sr.ROUTE_EXAMPLES.items() if t in exs)
            out.append([scores.get(route, 0.0), 0.0])
        return out
    return encode


def test_명확한_1위는_그_경로를_고른다():
    enc = _fake_encode({"payout": 0.95, "terms": 0.40})
    got = sr.route_semantic("__QUERY__", encode=enc)
    assert got is not None
    assert got.route == "payout"
    assert got.score == pytest.approx(0.95)
    assert got.runner_up == "terms"


def test_최고점수가_임계_미만이면_기권():
    low = SEMANTIC_ROUTE_MIN_SCORE - 0.05
    enc = _fake_encode({"payout": low, "terms": 0.0})
    assert sr.route_semantic("__QUERY__", encode=enc) is None


def test_1위와_2위가_팽팽하면_기권한다():
    """판정불가 — 잘못 고르면 결정론 경로가 확신에 찬 오답을 낸다."""
    top = SEMANTIC_ROUTE_MIN_SCORE + 0.2
    enc = _fake_encode({"payout": top, "terms": top - (SEMANTIC_ROUTE_MIN_MARGIN / 2)})
    assert sr.route_semantic("__QUERY__", encode=enc) is None


def test_margin이_임계_이상이면_통과():
    top = SEMANTIC_ROUTE_MIN_SCORE + 0.2
    enc = _fake_encode({"payout": top, "terms": top - (SEMANTIC_ROUTE_MIN_MARGIN * 2)})
    got = sr.route_semantic("__QUERY__", encode=enc)
    assert got is not None and got.route == "payout"


def test_빈_질의는_None():
    assert sr.route_semantic("", encode=_fake_encode({"payout": 0.99})) is None
    assert sr.route_semantic("   ", encode=_fake_encode({"payout": 0.99})) is None


def test_예시_임베딩은_1회만_계산한다():
    """캐시 — 요청마다 예시 수십 개를 다시 임베딩하면 라우팅이 검색보다 비싸진다."""
    calls = []
    base = _fake_encode({"payout": 0.95})

    def counting(texts):
        calls.append(len(texts))
        return base(texts)

    sr.route_semantic("__QUERY__", encode=counting)
    sr.route_semantic("__QUERY__", encode=counting)
    # 1회차: 질의 1 + 예시 N, 2회차: 질의 1 만
    assert calls[0] == 1 and calls[1] > 1, "첫 호출이 예시를 임베딩해야 한다"
    assert calls[2] == 1 and len(calls) == 3, "두 번째 호출은 질의만 임베딩해야 한다"


def test_모든_경로에_예시가_있다():
    """경로를 추가하고 예시를 빼먹으면 그 경로는 영원히 선택되지 않는다."""
    for route, examples in sr.ROUTE_EXAMPLES.items():
        assert len(examples) >= 3, f"{route}: 예시가 너무 적다"


def test_예시는_경로간_중복이_없다():
    """같은 문장이 두 경로에 있으면 그 둘의 margin 이 0 이라 영구 기권한다."""
    seen: dict[str, str] = {}
    for route, examples in sr.ROUTE_EXAMPLES.items():
        for ex in examples:
            assert ex not in seen, f"'{ex}' 가 {seen.get(ex)} 와 {route} 에 중복"
            seen[ex] = route
