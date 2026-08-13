"""SQL 경로 coverage_sql 순수 로직 — 별표3 ICD 3-값 보장판정.

judge_coverage(코드, ranges, 담보) → 보장/미보장(+리다이렉트)/판정불가. golden_coverage와
동일 케이스를 픽스처로 재현(담보특정성·제외우선·판정불가·리다이렉트).
"""

from src.v1.rag.coverage_sql import (
    is_coverage_query, extract_code, extract_coverage, judge_coverage, format_coverage,
    effective_coverage,
)

# 다이렉트 별표3 코드범위 (coverage_range 적재본 축약)
_RANGES = {
    "암진단자금": ["C00~C14", "C15~C26", "C50", "C81~C96", "C97", "D45", "D47.1"],
    "제자리암진단자금": ["D00", "D01", "D05", "D09"],
    "경계성종양진단자금": ["D37", "D41", "D48"],
}


def test_coverage_gate_fires_on_coverage_queries():
    for q in ["C50은 보장되나요?", "위암 보장돼요?", "이 코드 보장 여부는?"]:
        assert is_coverage_query(q), f"보장판정 질의 미감지: {q}"


def test_coverage_gate_excludes_amount_and_terms():
    for q in ["중환자실 하루 얼마?", "청약철회 언제까지?"]:
        assert not is_coverage_query(q), f"비-보장판정 질의가 샘: {q}"


def test_extract_code_explicit_and_disease_map():
    assert extract_code("C50 유방암 보장?") == "C50"
    assert extract_code("D47.1 보장되나요?") == "D47.1"
    assert extract_code("위암은 보장돼요?") == "C16"        # 병명맵
    assert extract_code("두통은 보장돼요?") is None         # 미매핑 → RAG


def test_judge_covered():
    assert judge_coverage("C50", _RANGES, "암진단자금")["verdict"] == "보장"
    assert judge_coverage("C16", _RANGES, "암진단자금")["verdict"] == "보장"   # C15~C26


def test_judge_uncovered_redirects_by_specificity():
    """D05를 암진단자금으로 물으면 미보장 + 제자리암으로 리다이렉트(담보특정성)."""
    v = judge_coverage("D05", _RANGES, "암진단자금")
    assert v["verdict"] == "미보장" and v["redirect_coverage"] == "제자리암진단자금"


def test_judge_same_code_covered_under_correct_coverage():
    assert judge_coverage("D05", _RANGES, "제자리암진단자금")["verdict"] == "보장"


def test_judge_undetermined_is_precision_first():
    """어느 범위에도 없으면 억지 판정 대신 판정불가."""
    assert judge_coverage("Z99", _RANGES, "암진단자금")["verdict"] == "판정불가"


def test_judge_without_coverage_finds_matching():
    """담보 미지정 — 코드를 담은 담보를 찾아 보장."""
    v = judge_coverage("D41", _RANGES, None)
    assert v["verdict"] == "보장" and v["coverage"] == "경계성종양진단자금"


def test_format_no_code_returns_rag_signal():
    assert "→RAG" in format_coverage(None, None)


def test_effective_coverage_for_reconcile():
    """reconcile용 실제 지급 담보 — 보장=그 담보, 미보장=리다이렉트, 판정불가=None."""
    assert effective_coverage(judge_coverage("C50", _RANGES, "암진단자금")) == "암진단자금"
    assert effective_coverage(judge_coverage("D05", _RANGES, "암진단자금")) == "제자리암진단자금"  # 리다이렉트
    assert effective_coverage(judge_coverage("Z99", _RANGES, "암진단자금")) is None
