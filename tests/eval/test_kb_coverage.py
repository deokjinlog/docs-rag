"""KB 암 별표3 범위 뺄셈(순수 로직) 잠금 — 유사암 제외로 담보 특정성.

핵심 계약: 암진단비 = 악성신생물 − {C44, C73}(약관 제3조 명시). C43~C44에서 C44만 빼 C43은 남고,
C73~C75에서 C73만 빼 C74~C75가 남는 범위 뺄셈이 정확해야 한다(흑색종 C43 보장·갑상선암 C73 제외).
파일(raw md) 읽는 경로는 자립 make check(golden_kb_coverage)가 커버 — 여기선 순수 뺄셈만.
"""
from scripts.extract_kb_coverage import _expand_c, _collapse


def test_expand_c_range_and_single():
    assert _expand_c("C43~C44") == [43, 44]
    assert _expand_c("C50") == [50]
    assert _expand_c("C00~C14") == list(range(0, 15))
    assert _expand_c("D45") == []          # D코드는 C 확장 대상 아님


def test_collapse_merges_and_splits():
    assert _collapse([43, 45, 46, 47]) == ["C43", "C45~C47"]
    assert _collapse([0, 1, 2]) == ["C00~C02"]


def test_subtraction_keeps_c43_drops_c44_c73():
    """C43~C44에서 C44만, C73~C75에서 C73만 제거되는지(범위 뺄셈 정확성)."""
    cats = _expand_c("C43~C44") + _expand_c("C73~C75")   # [43,44,73,74,75]
    kept = _collapse([c for c in cats if c not in {44, 73}])
    assert kept == ["C43", "C74~C75"]                   # C43 보존·C44/C73 제거
