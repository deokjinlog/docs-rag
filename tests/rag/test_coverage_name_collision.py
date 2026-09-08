"""담보명 포함관계 오매칭 회귀 테스트 — "암진단자금" ⊂ "갑상선암진단자금".

2026-09-07 실측 사고: `_COVERAGE_KEYWORDS` 에 "암진단자금"만 있고 갑상선암·기타피부암이
없어서, "갑상선암진단자금 90일이하 얼마?" 가 **암진단자금의 50%**를 답했다. 정답은 10% —
**5배 오답**이고 소비자가 자기 담보 아닌 지급률을 받는다.

고치는 과정에서 두 번 헛짚었다:
  ① 목록 순서만 바꿈    → 순서 실수에 계속 취약
  ② 길이 내림차순 정렬  → "갑상선암"(4자) < "암진단자금"(5자) 이라 여전히 짐.
                          게다가 "제자리암"(4자)도 같은 이유로 **새로 깨졌다**
  ③ 전체 담보명을 목록에 넣고 길이 정렬 → 통과
이 테스트가 ①②로 되돌아가는 걸 막는다.
"""
import pytest

from src.v1.rag.payout_sql import extract_payout_intent, select_payout


def _rows():
    """DIRECT_INPT_2024 의 실제 payout_rule 축약 — 포함관계 담보가 함께 있는 게 요점."""
    return [
        {"coverage": "암진단자금", "period_bucket": "90일이하", "rate_pct": 50,
         "per_unit": None, "limit_days": None},
        {"coverage": "암진단자금", "period_bucket": "1년이상", "rate_pct": 100,
         "per_unit": None, "limit_days": None},
        {"coverage": "갑상선암진단자금", "period_bucket": "90일이하", "rate_pct": 10,
         "per_unit": None, "limit_days": None},
        {"coverage": "갑상선암진단자금", "period_bucket": "1년이상", "rate_pct": 20,
         "per_unit": None, "limit_days": None},
        {"coverage": "기타피부암진단자금", "period_bucket": "1년이상", "rate_pct": 20,
         "per_unit": None, "limit_days": None},
        {"coverage": "제자리암진단자금", "period_bucket": "90일이하", "rate_pct": 10,
         "per_unit": None, "limit_days": None},
        {"coverage": "경계성종양진단자금", "period_bucket": "1년이상", "rate_pct": 20,
         "per_unit": None, "limit_days": None},
    ]


@pytest.mark.parametrize("query,expected_coverage", [
    ("갑상선암진단자금 90일이하 얼마?", "갑상선암진단자금"),
    ("기타피부암진단자금 1년이상 얼마?", "기타피부암진단자금"),
    ("제자리암진단자금 90일이하 얼마?", "제자리암진단자금"),
    ("경계성종양진단자금 1년이상 얼마?", "경계성종양진단자금"),
    ("암진단자금 1년이상 얼마?", "암진단자금"),
])
def test_포함관계_담보는_더_구체적인_쪽이_이긴다(query, expected_coverage):
    intent = extract_payout_intent(query)
    assert intent.get("coverage") == expected_coverage, (
        f"'{query}' → {intent.get('coverage')!r} (기대 {expected_coverage!r}). "
        "짧은 담보명이 먼저 걸리면 5배 틀린 지급률이 나간다"
    )


@pytest.mark.parametrize("query,rate", [
    ("갑상선암진단자금 90일이하 얼마?", 10),
    ("기타피부암진단자금 1년이상 얼마?", 20),
    ("제자리암진단자금 90일이하 얼마?", 10),
    ("경계성종양진단자금 1년이상 얼마?", 20),
    ("암진단자금 1년이상 얼마?", 100),
    ("암진단자금 90일이하 얼마?", 50),
])
def test_선택된_지급률이_그_담보의_값이다(query, rate):
    hit = select_payout(_rows(), query)
    assert hit is not None, f"'{query}' 가 결정론 답을 못 냈다"
    assert hit["rate_pct"] == rate, (
        f"'{query}' → {hit['coverage']} {hit['rate_pct']}% (기대 {rate}%)"
    )


def test_줄여_물어도_담보가_해소된다():
    """'갑상선암 얼마?' 처럼 '진단자금'을 빼고 물어도 잡혀야 한다."""
    assert extract_payout_intent("갑상선암 90일이하 얼마?")["coverage"] == "갑상선암"
    assert select_payout(_rows(), "갑상선암 90일이하 얼마?")["rate_pct"] == 10


def test_정확일치가_부분일치를_이긴다():
    """intent 가 '암진단자금'이면 갑상선암진단자금 행이 후보에 섞이면 안 된다."""
    hit = select_payout(_rows(), "암진단자금 1년이상 얼마?")
    assert hit["coverage"] == "암진단자금"


def test_목록_순서에_의존하지_않는다():
    """순서를 뒤집어도 결과가 같아야 한다 — 정렬로 방어하는 게 요점."""
    import src.v1.rag.payout_sql as ps
    original = ps._COVERAGE_KEYWORDS
    try:
        ps._COVERAGE_KEYWORDS = list(reversed(original))
        assert extract_payout_intent("갑상선암진단자금 얼마?")["coverage"] == "갑상선암진단자금"
    finally:
        ps._COVERAGE_KEYWORDS = original
