"""파싱 구조 무결성 판정(parse_golden._structure) 순수 로직.

`structure` 골든 필드가 파서 회귀를 실제로 잡는지 못박는다 — 1..N 갭·중복 조번호·빈 제목을
정확히 서술하고, 정상이면 'clean'. 이게 골든 수준의 '파싱 안정도' 잠금(gate.py sanity 승격).
회귀 그물은 깨질 때 깨져야 가치 있으므로, 각 이상 유형이 감지됨을 직접 검증.
"""
from scripts.parse_golden import _structure


def _c(jo, title="제목"):
    return {"jo": jo, "title": title}


def test_clean_when_continuous_unique_titled():
    clauses = [_c(1, "목적"), _c(2, "정의"), _c(3, "지급사유")]
    assert _structure(clauses) == "clean"


def test_detects_gap():
    """1..N 연속이 깨지면(빠진 조) gap으로 잡는다 — 파싱 누락/병합 회귀."""
    assert _structure([_c(1), _c(2), _c(4)]) == "gap:[3]"


def test_detects_duplicate_jo():
    """같은 조번호 중복(과탐·경계 오인식) → dup."""
    assert _structure([_c(1), _c(2), _c(2), _c(3)]) == "dup:[2]"


def test_detects_blank_title():
    """제목이 비면(제목 파싱 실패) → blank."""
    assert _structure([_c(1, "목적"), _c(2, "  "), _c(3, "지급")]) == "blank:[2]"


def test_detects_multiple_anomalies_together():
    v = _structure([_c(1, ""), _c(2), _c(4)])
    assert "gap:[3]" in v and "blank:[1]" in v


def test_empty_clause_list_is_not_clean():
    """조 0개(파싱 전면 실패)는 clean 아님 — 조용한 빈 파싱을 막는다."""
    assert _structure([]) != "clean"


def test_single_clause_is_clean():
    assert _structure([_c(1, "목적")]) == "clean"
