"""경로 의도 분류 — 임베딩 유사도 기반. LLM 호출 없음.

`is_*_query()` 정규식 게이트가 못 잡는 표현을 받는 **2단계 라우터**다
(classifier.py 독스트링의 "3단계 Semantic Router" 자리).

왜 정규식만으로 부족한가 — 정규식은 `eval_sql_routing` 31/31로 정확하지만 **어휘가
고정**이다. `_COVERAGE_KEYWORDS` 같은 리스트에 없는 표현이 오면 조용히 RAG로 떨어진다.
새 담보·새 말투마다 코드를 고쳐야 하고, 그게 커버리지 상한이다. 임베딩 라우터는 같은
일을 **예시 문장 추가**로 한다 — 코드 수정 없음.

왜 LLM 라우터가 아닌가 — 정규식이 이미 1.00인 계층에 LLM을 얹으면 정확도는 못 올리고
지연·비용·비결정성만 는다. 임베딩은 BGE-M3가 **이미 떠 있어** 추가 비용이 사실상 0이고
(질의 1건 임베딩 ≈ 200ms, 예시는 1회 캐시), vLLM 없이도 검증된다.

**precision-first 2중 안전**은 그대로다:
    ① 최고 점수가 낮으면            → None (아무 경로도 확신 못 함)
    ② 1·2위가 팽팽하면(margin 미달)  → None (판정불가 — judge_coverage와 같은 3-값 철학)
    None 이면 기존 흐름(정규식 결과 or RAG)이 받는다. 억지 라우팅을 하지 않는다.

또 하나 — 이 라우터는 **의도(intent)만** 맞힌다. "골든라이프 중환자실 하루 얼마?"는
의도가 payout으로 맞지만 KB에 그 담보가 없어 최종 경로는 RAG다. 그건 의도 오류가 아니라
**데이터 가용성** 문제고, `select_payout` 이 None 을 내는 기존 폴백이 이미 처리한다.
그래서 여기의 채점 대상은 최종 route 가 아니라 intent 다(`eval_semantic_route.py`).
"""

import threading
from dataclasses import dataclass

from ..config.settings import (
    SEMANTIC_ROUTE_MIN_MARGIN,
    SEMANTIC_ROUTE_MIN_SCORE,
)

# 경로별 예시 발화.
#
# ⚠ **골든 질의를 그대로 쓰지 않는다.** 골든(`golden_sql_routing.jsonl`)으로 채점하는데
# 예시에 같은 문장을 넣으면 train-on-test 가 되어 점수가 무의미해진다. 의도는 같되 표현이
# 다른 문장으로 채웠다.
#
# 중심(centroid)이 아니라 **예시별 최댓값**으로 매칭한다 — 한 경로가 의미적으로 여러
# 덩어리이기 때문이다(terms = 청약철회 ∪ 갱신 ∪ 만기). 평균을 내면 세 덩어리 사이의
# 빈 공간에 중심이 앉아 어디에도 안 가깝다.
ROUTE_EXAMPLES: dict[str, tuple[str, ...]] = {
    # 얼마 / 지급률 / 감액 / 한도 — 값이 정해진 지급액
    "payout": (
        "입원하면 하루에 얼마 나와요?",
        "진단받으면 보험금 얼마 지급되나요?",
        "지급률이 몇 퍼센트인가요?",
        "가입금액의 몇 프로를 받나요?",
        "1년 안에 걸리면 얼마나 깎이나요?",
        "지급 한도가 며칠까지인가요?",
        "수술받으면 얼마 받을 수 있어요?",
        "월마다 얼마씩 나오나요?",
    ),
    # 청약철회 / 갱신 / 만기 — 계약의 "언제까지"
    "terms": (
        "청약을 철회할 수 있는 기간이 어떻게 되나요?",
        "며칠 안에 취소할 수 있나요?",
        "이 상품 갱신형인가요?",
        "갱신 주기가 몇 년인가요?",
        "보험기간이 몇 년 만기인가요?",
        "만기가 언제까지예요?",
    ),
    # 별표3 ICD 코드 보장 판정 — 코드·병명이 보장 범위에 드나
    "coverage": (
        "이 질병코드는 보장 대상인가요?",
        "해당 코드로 보험금 받을 수 있나요?",
        "폐암도 보장되나요?",
        "제자리암은 보장 범위에 들어가나요?",
        "질병분류표에 포함된 병인가요?",
        "분류번호가 보장 범위 안에 드나요?",
    ),
    # 담보 멤버십 — 이 상품에 그 담보가 붙어 있나
    "catalog": (
        "이 상품에 뇌졸중 담보가 있나요?",
        "치매진단비 특약이 들어있어요?",
        "골절 보장해주는 담보 있어?",
        "어떤 담보들이 포함돼 있나요?",
    ),
    # 면책기간 / 감액기간 — 가입 후 "언제부터 온전히"
    #
    # ⚠ 여기 예시는 전부 **시간축**이어야 한다. 초기엔 "면책기간이 얼마나 되나요?"를 넣었다가
    # "이 특약은 뭐가 면책인가요?"(=면책 **사유**)를 waiting 으로 끌어갔다. 한국어 보험
    # 도메인에서 '면책'은 기간(waiting)과 사유(exclusion) 두 뜻을 겹쳐 쓰는 함정어라,
    # waiting 쪽은 '기간·개시일·경과' 같은 시간 신호로만 표현한다.
    "waiting": (
        "가입하고 며칠 지나야 보장되나요?",
        "보장개시일이 언제부터인가요?",
        "가입 직후에 진단받아도 보장받나요?",
        "처음 1년은 절반만 지급되나요?",
        "가입 후 몇 개월간은 보장에서 빠지나요?",
        "보장제외기간이 며칠인가요?",
    ),
    # 면책 사유 — 어떤 경우에 못 받나
    "exclusion": (
        "어떤 경우에 보험금을 못 받나요?",
        "지급하지 않는 사유가 무엇인가요?",
        "면책 사유를 알려주세요",
        "보상하지 않는 손해는 무엇인가요?",
        "면책되는 경우에는 어떤 것들이 있나요?",
        "보험금이 지급 거절되는 조건은 무엇인가요?",
    ),
    # 해석 · 절차 · 정의 — 결정론 값이 아니라 조문을 읽어야 하는 것
    #
    # ⚠ "언제 지급되나요"(지급사유가 성립하는 조건)는 payout("얼마")이 아니라 여기다.
    # 담보명이 겹쳐 payout 으로 새기 쉬워 시점·조건형 예시를 명시적으로 둔다 —
    # 정규식 `is_payout_amount_query` 가 같은 이유로 '얼마/지급률'만 통과시킨다.
    "rag": (
        "이 용어는 어떻게 정의되나요?",
        "그 단어의 뜻이 무엇인가요?",
        "보험금 청구 절차가 어떻게 되나요?",
        "청구할 때 필요한 서류가 무엇인가요?",
        "어떤 경우에 지급사유가 성립하나요?",
        "이 급여금은 언제 지급되나요?",
        "어떤 조건을 충족해야 지급되나요?",
        # exclusion 과의 경계: 저쪽은 **목록 요청**("뭐가 면책이냐"), 이쪽은 **특정 상황
        # 판단**("이 경우엔 되냐"). 상황형은 사유 목록을 덤프해봐야 답이 안 되고 조문
        # 해석이 필요하다 — 그래서 rag.
        "이런 상황에서도 보험금이 나오나요?",
        "그런 사고로 다쳐도 보상받을 수 있나요?",
        "계약이 해지되면 어떻게 되나요?",
        "보험료를 연체하면 어떻게 되나요?",
    ),
}


@dataclass
class SemanticRoute:
    """의도 분류 결과. `route`는 ROUTE_EXAMPLES 의 키."""
    route: str
    score: float            # 최고 유사도 (정규화 임베딩이라 코사인 = 내적)
    margin: float           # 1위 − 2위. 작을수록 판정불가에 가깝다
    runner_up: str | None   # 2위 경로 — 오분류 진단용


# 예시 임베딩은 정적이라 1회만 계산하고 캐시한다.
#
# Why 락: api 는 uvicorn 워커, celery 는 `--pool=threads`라 락 없는 lazy init 은 여러
# 스레드가 `is None` 을 동시에 통과해 BGE-M3 encode 를 N번 돌린다. embedding.py 가
# 같은 이유로 이미 쓰는 double-checked locking 패턴을 그대로 따른다.
_cache: list[tuple[str, list[float]]] | None = None
_cache_lock = threading.Lock()


def _build_cache(encode) -> list[tuple[str, list[float]]]:
    labels: list[str] = []
    texts: list[str] = []
    for route, examples in ROUTE_EXAMPLES.items():
        for ex in examples:
            labels.append(route)
            texts.append(ex)
    return list(zip(labels, encode(texts)))


def _get_cache(encode) -> list[tuple[str, list[float]]]:
    global _cache
    if _cache is not None:          # fast path
        return _cache
    with _cache_lock:
        if _cache is None:          # 락 획득 시점 재확인
            _cache = _build_cache(encode)
    return _cache


def reset_cache() -> None:
    """테스트에서 다른 encode 를 주입할 때 캐시를 비운다."""
    global _cache
    with _cache_lock:
        _cache = None


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def route_semantic(query: str, encode=None) -> SemanticRoute | None:
    """질의의 경로 의도를 분류한다. 확신 못 하면 None.

    `encode`: `list[str] -> list[list[float]]` (정규화된 임베딩). 기본은 BGE-M3.
    주입 가능하게 둔 건 단위 테스트가 모델 로드 없이 돌게 하려는 것 —
    CLAUDE.md "heavy 자원 모듈은 사용처가 직접 import" 규율과 같은 이유다.
    """
    q = (query or "").strip()
    if not q:
        return None
    if encode is None:
        from ..utils.embedding import embed_texts   # heavy — 호출 시점에 import
        encode = embed_texts

    qv = encode([q])[0]
    best: dict[str, float] = {}
    for route, vec in _get_cache(encode):
        s = _dot(qv, vec)
        if s > best.get(route, -1.0):
            best[route] = s          # 경로별 최근접 예시 점수

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    top_route, top_score = ranked[0]
    runner_up, second = (ranked[1] if len(ranked) > 1 else (None, 0.0))
    margin = top_score - second

    # ① 아무것도 충분히 안 닮았다 → 억지로 고르지 않는다
    if top_score < SEMANTIC_ROUTE_MIN_SCORE:
        return None
    # ② 1·2위가 팽팽하다 → 판정불가. 잘못 고르면 결정론 경로가 틀린 답을 확신 있게 낸다
    if margin < SEMANTIC_ROUTE_MIN_MARGIN:
        return None
    return SemanticRoute(route=top_route, score=top_score,
                         margin=margin, runner_up=runner_up)
