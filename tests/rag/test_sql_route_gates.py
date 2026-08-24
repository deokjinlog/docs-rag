"""SQL 6경로 게이트 상호작용 잠금 — 게이트가 6개로 늘어 상호작용이 실질 회귀 위험.

/answer 라우팅 순서: payout → terms → coverage → catalog → waiting → exclusion → RAG.
첫 매칭 게이트가 이긴다(단, 매칭돼도 데이터 미해소면 다음으로 폴백 — 그건 데이터 의존이라
`golden_sql_routing`(스택)이 검증). 여기선 **게이트 발동 집합·순서 계약**을 스택 없이 못박아,
새 게이트/키워드 추가 시 의도치 않은 오버랩을 CI에서 잡는다.
"""

from src.v1.rag.payout_sql import is_payout_amount_query
from src.v1.rag.terms_sql import is_terms_query
from src.v1.rag.coverage_sql import is_coverage_query
from src.v1.rag.catalog_sql import is_catalog_query
from src.v1.rag.waiting_sql import is_waiting_query
from src.v1.rag.exclusion_sql import is_exclusion_query

# 라우터 검사 순서 그대로(첫 매칭 우선)
ORDER = [
    ("payout", is_payout_amount_query),
    ("terms", is_terms_query),
    ("coverage", is_coverage_query),
    ("catalog", is_catalog_query),
    ("waiting", is_waiting_query),
    ("exclusion", is_exclusion_query),
]


def _fired(q):
    return [n for n, g in ORDER if g(q)]


def _first(q):
    f = _fired(q)
    return f[0] if f else "RAG"


def test_each_gate_fires_on_canonical():
    """각 경로의 대표 질의가 그 게이트를 켠다."""
    assert is_payout_amount_query("중환자실 하루 얼마 받아요?")
    assert is_terms_query("청약철회 언제까지 가능한가요?")
    assert is_coverage_query("C50 유방암 보장되나요?")
    assert is_catalog_query("골든라이프에 파킨슨병진단비 담보 있어?")
    assert is_waiting_query("암진단비 면책기간 얼마야?")
    assert is_exclusion_query("이 특약은 뭐가 면책인가요?")


def test_first_gate_is_intended_primary():
    """첫 매칭(=주 처리기)이 의도대로. 폴백 케이스는 별도 테스트."""
    assert _first("청약철회 언제까지 가능한가요?") == "terms"
    assert _first("C50 유방암 보장되나요?") == "coverage"          # 코드 있으면 coverage
    assert _first("골든라이프에 파킨슨병진단비 담보 있어?") == "catalog"
    assert _first("이 특약은 뭐가 면책인가요?") == "exclusion"
    assert _first("보험금 청구 절차 어떻게 해요?") == "RAG"          # 결정론 아님


def test_clean_non_collisions():
    """오염 방지 — 순수 절차/해석 질의가 결정론 게이트로 새지 않음."""
    assert _fired("보험금 청구 절차 어떻게 해요?") == []
    assert not is_payout_amount_query("청약철회 언제까지?")          # 언제≠얼마
    assert not is_catalog_query("중환자실 하루 얼마 받아요?")        # 얼마≠담보존재
    assert not is_exclusion_query("골든라이프에 파킨슨병진단비 담보 있어?")


def test_documented_overlaps():
    """알려진 오버랩(순서/폴백으로 해소) — 계약으로 못박음.

    · '보장돼?'는 coverage·catalog 둘 다 켜짐 → 순서상 coverage 우선(코드 있으면 판정, 없으면
      coverage 브랜치가 코드 미검출로 스킵→catalog로 폴백. 그 폴백은 데이터 의존=스택 골든 소관).
    · '감액'은 payout·waiting 둘 다 켜짐 → payout 우선이나 KB는 기저율 없어 select_payout miss→
      waiting 폴백(데이터 의존). 게이트 레벨에선 둘 다 켜짐을 확인.
    """
    assert set(_fired("C73 암진단비 보장돼?")) >= {"coverage", "catalog"}
    assert _fired("C73 암진단비 보장돼?")[0] == "coverage"          # 순서상 coverage 먼저
    assert set(_fired("암진단비 감액 있어?")) >= {"payout", "waiting"}
    assert _fired("암진단비 감액 있어?")[0] == "payout"             # payout 먼저(miss시 waiting 폴백)
