"""SQL 경로 payout_sql 순수 로직 — 결정론 '얼마/언제' 답변.

payout_rule row 픽스처를 주입해 extract_payout_intent + select_payout + format_payout를
검증. 실 DB(PayoutRepository.get_rules) 대신 픽스처라 스택 불필요. golden_payout_qa.jsonl
5케이스를 픽스처로 재현 — SQL 경로 로직의 무회귀 게이트.
"""

from src.v1.rag.payout_sql import extract_payout_intent, select_payout, format_payout


# payout_rule row 픽스처 (골든 5케이스 커버)
_ROWS = [
    {"coverage": "중환자실 입원급여금", "cause": None, "age_band": None,
     "period_bucket": None, "rate_pct": 1, "per_unit": "1일당", "limit_days": 10},
    {"coverage": "레진충전", "cause": "질병", "age_band": None,
     "period_bucket": "90일이하", "rate_pct": 0},
    {"coverage": "레진충전", "cause": "질병", "age_band": None,
     "period_bucket": "1년이상", "rate_pct": 50},
    {"coverage": "암진단자금", "cause": None, "age_band": "15세이상",
     "period_bucket": "90일이하", "rate_pct": 0},
    {"coverage": "암진단자금", "cause": None, "age_band": "15세이상",
     "period_bucket": "1년이상", "rate_pct": 100},
    {"coverage": "소득보장 12개월", "cause": None, "age_band": None,
     "period_bucket": None, "rate_pct": 30, "per_unit": "매월"},
    {"coverage": "소득보장 6개월", "cause": None, "age_band": None,
     "period_bucket": None, "rate_pct": 20, "per_unit": "매월"},
]

# golden_payout_qa.jsonl 5케이스
_GOLDEN = [
    ("중환자실 입원하면 하루 얼마 받아요?", 1),
    ("레진 충전 질병으로 1년 지나서 받으면 얼마?", 50),
    ("레진 충전 질병 90일 안에 받으면 얼마?", 0),
    ("암진단자금 15세 이상이 90일 이내 진단이면 얼마?", 0),
    ("12개월 소득보장 수술 받으면 매월 얼마?", 30),
]


def test_payout_qa_golden_5_of_5():
    """골든 5케이스 전부 결정론 rate 일치 — SQL 경로 로직 무회귀 게이트."""
    for query, expect in _GOLDEN:
        r = select_payout(_ROWS, query)
        assert r is not None, f"미스: {query}"
        assert r["rate_pct"] == expect, f"{query} → {r['rate_pct']} (기대 {expect})"


def test_intent_cause_negation():
    """'재해 아닌'=질병(부정 처리), '재해/상해'=상해."""
    assert extract_payout_intent("재해 아닌 병으로")["cause"] == "질병"
    assert extract_payout_intent("상해로 다쳐서")["cause"] == "상해"


def test_coverage_token_overlap_disambiguates():
    """12개월 vs 6개월 — 담보 토큰 겹침으로 구분(둘 다 '소득보장' 매칭이지만 12개월 우선)."""
    r = select_payout(_ROWS, "12개월 소득보장 매월 얼마?")
    assert r["rate_pct"] == 30 and "12개월" in r["coverage"]


def test_causeless_axis_skipped():
    """중환자실은 cause 축을 안 쓰므로, cause 의도가 있어도 필터 스킵(오히려 미스 방지)."""
    r = select_payout(_ROWS, "중환자실 재해로 입원하면 하루 얼마?")
    assert r is not None and r["coverage"] == "중환자실 입원급여금"


def test_no_match_returns_none_and_rag_signal():
    """매칭 없으면 None → format이 RAG 폴백 신호."""
    assert select_payout(_ROWS, "청약철회 언제까지?") is None
    assert "→RAG" in format_payout(None)


def test_format_includes_rate_and_limit():
    r = select_payout(_ROWS, "중환자실 하루 얼마?")
    out = format_payout(r)
    assert "1%" in out and "한도 10일" in out
