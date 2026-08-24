"""SQL 경로 catalog_sql 순수 로직 — 담보 존재 멤버십("이 상품에 X 담보 있어?").

핵심 계약: (1) 멤버십 의도 게이트가 '담보 있어/보장돼'는 잡고 '얼마'는 안 잡음, (2) 특약 이름
정규화가 번호·괄호·갱신변형을 벗김, (3) 매칭은 담보명(≥4자·비일반어) 부분일치만 — 못 찾으면
부재를 단정하지 않고 RAG(precision-first).
"""

from src.v1.rag.catalog_sql import (
    is_catalog_query, normalize_coverage, find_covered, format_catalog,
)


def test_catalog_gate_fires_on_membership_queries():
    for q in ["이 상품에 파킨슨병진단비 담보 있어?", "골든라이프 뭐 보장해?",
              "암진단비 보장되나요?", "무슨 담보 있나요?"]:
        assert is_catalog_query(q), f"멤버십 질의 미감지: {q}"


def test_catalog_gate_excludes_amount():
    """'얼마'(payout)는 catalog 게이트가 안 잡음(라우팅 배타)."""
    assert not is_catalog_query("암진단비 얼마 받아요?")
    assert not is_catalog_query("중환자실 하루 얼마?")


def test_normalize_coverage_strips_prefix_parens_variant():
    assert normalize_coverage("1. 장기요양간병비(1~5급)(간편가입)") == "장기요양간병비"
    assert normalize_coverage("8-1. 상해입원일당Ⅱ") == "상해입원일당Ⅱ"
    assert normalize_coverage("4. 간병인사용 상해입원일당【갱신계약】") == "간병인사용 상해입원일당"


def test_find_covered_matches_named_coverage():
    catalog = ["1. 파킨슨병진단비(간편가입)", "2. 암진단비(간편가입)", "3. 장기요양간병비(1~5급)(간편가입)"]
    assert find_covered(catalog, "골든라이프에 파킨슨병진단비 담보 있어?") == ["파킨슨병진단비"]
    assert find_covered(catalog, "암진단비 보장돼?") == ["암진단비"]


def test_find_covered_rejects_generic_and_missing():
    """일반어('상해')·미적중은 매칭 안 함(precision — 부재 단정 안 함)."""
    catalog = ["1. 상해수술비(간편가입)", "2. 암진단비(간편가입)"]
    assert find_covered(catalog, "상해 보장돼?") == []          # '상해' 일반어 단독 → 미적중
    assert find_covered(catalog, "치아보철 담보 있어?") == []    # catalog에 없음 → 미적중


def test_format_catalog_miss_returns_rag_signal():
    assert "→RAG" in format_catalog([])
    assert "파킨슨병진단비" in format_catalog(["파킨슨병진단비"])
