"""KB 면책기간·감액 파싱(순수 로직) 잠금 — 2열 표 조건 문자열 → 결정론 값.

핵심 계약: (1) '가입 후 N일간 보장 제외'만 면책기간(재진단암 '진단 후 N년간'=다른 개념 제외),
(2) 감액 주 티어 '가입 후 1년간 M% 지급' + 서브 '(단,K일미만 L%)', (3) 매칭 없으면 None
(precision-first). 파일(raw md) 읽는 경로는 자립 make check(golden_waiting)가 커버.
"""
from scripts.extract_waiting import parse_cond


def test_waiting_only_gaib_days():
    """'가입 후 N일간 보장 제외' → waiting_days=N."""
    assert parse_cond("가입 후 90일간 보장 제외")["waiting_days"] == 90
    assert parse_cond("가입후 90일간 보장 제외(단, 갑상선암은 제외)")["waiting_days"] == 90


def test_reduction_main_and_sub_tier():
    r = parse_cond("가입 후 1년간 보험금 50% 지급 (단, 90일미만 10% 지급)")
    assert r["reduction_period"] == "1년이내" and r["reduction_rate_pct"] == 50
    assert r["sub_period_days"] == 90 and r["sub_rate_pct"] == 10


def test_reduction_nonstandard_rate():
    """비-50 감액률도 실측대로(암수술비Ⅱ 40%)."""
    assert parse_cond("가입후 1년간 보험금 40% 지급")["reduction_rate_pct"] == 40


def test_rediagnosis_interval_not_waiting():
    """'진단 후 N년간 보장 제외'(재진단 간격)는 가입 면책기간 아님 → None(precision)."""
    r = parse_cond("첫 번째 암 진단 후 2년간 보장 제외")
    assert r["waiting_days"] is None


def test_no_match_returns_none():
    r = parse_cond("보험금을 전액 지급합니다")
    assert r["waiting_days"] is None and r["reduction_rate_pct"] is None
