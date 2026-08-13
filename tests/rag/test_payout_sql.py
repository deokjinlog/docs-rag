"""SQL 경로 payout_sql 순수 로직 — 결정론 '얼마/언제' 답변.

payout_rule row 픽스처를 주입해 extract_payout_intent + select_payout + format_payout를
검증. 실 DB(PayoutRepository.get_rules) 대신 픽스처라 스택 불필요. golden_payout_qa.jsonl
5케이스를 픽스처로 재현 — SQL 경로 로직의 무회귀 게이트.
"""

from src.v1.rag.payout_sql import (
    extract_payout_intent, select_payout, format_payout, is_payout_amount_query,
    format_exclusion_note, format_payout_complete, extract_exclusion_tags,
)


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


# ── amount 게이트 — /answer SQL 라우팅 precision (지급액 질의만 SQL) ──────────
def test_amount_gate_fires_on_payout_queries():
    """'얼마/지급률/감액' 등 결정론 지급값 질의 → SQL 라우팅."""
    for q in [
        "중환자실 입원하면 하루 얼마 받아요?",
        "레진 충전 질병 1년 후 지급률은?",
        "12개월 소득보장 매월 얼마?",
        "1년 이내 재해 외 원인이면 감액되나요?",
    ]:
        assert is_payout_amount_query(q), f"amount 질의 미감지: {q}"


def test_amount_gate_excludes_interpretation_queries():
    """담보를 언급해도 '지급사유·정의·절차·서류'를 묻는 해석 질의는 SQL로 새면 안 됨(RAG 소관)."""
    for q in [
        "중환자실 입원급여금은 언제 지급되나요?",   # 지급사유(해석) — 담보만 겹침
        "충치(치아우식증)는 어떻게 정의되나요?",     # 정의
        "보험금 청구 시 필요한 서류는?",             # 절차
        "소득보장수술은 무엇을 참조해 지급되나요?",   # 지급 근거(해석)
    ]:
        assert not is_payout_amount_query(q), f"해석 질의가 SQL로 샘: {q}"


# ── 면책 강제첨부 — "얼마?" 답에 지급 제외(면책) 항상 붙임(완결성) ──────────
def test_exclusion_note_attaches_clause_refs():
    note = format_exclusion_note([{"jo": 7, "title": "보험금을 지급하지 않는 사유"}])
    assert "면책" in note and "제7조" in note


def test_exclusion_note_empty_when_no_exclusions():
    assert format_exclusion_note([]) == ""


def test_payout_complete_combines_rate_and_exclusion():
    """완결 답 = 지급률 + 면책 강제첨부 한 줄."""
    rule = select_payout(_ROWS, "중환자실 하루 얼마?")
    out = format_payout_complete(rule, [{"jo": 7, "title": "보험금을 지급하지 않는 사유"}])
    assert "1%" in out and "면책" in out and "제7조" in out


def test_payout_complete_no_exclusion_is_bare_payout():
    rule = select_payout(_ROWS, "중환자실 하루 얼마?")
    assert format_payout_complete(rule, []) == format_payout(rule)


def test_payout_complete_miss_returns_rag_signal():
    assert "→RAG" in format_payout_complete(None, [])


def test_extract_exclusion_tags_from_body():
    """면책 조 본문 → 표준 사유 태그(고의·전쟁내란 등). 서빙=오프라인 골든 단일 소스."""
    tags = extract_exclusion_tags("1. 고의로 자신을 해친 경우 2. 전쟁, 내란, 폭동")
    assert "고의" in tags and "전쟁내란" in tags
    assert extract_exclusion_tags("") == []


def test_exclusion_note_uses_reason_tags_when_body_present():
    """body 있으면 조 참조 대신 실제 사유 태그를 노출(강제첨부 실질화)."""
    note = format_exclusion_note([
        {"jo": 7, "title": "보험금을 지급하지 않는 사유",
         "body": "1. 피보험자가 고의로 자신을 해친 경우"},
    ])
    assert "고의" in note and "제7조" in note and "면책" in note
