"""SQL 경로 terms_sql 순수 로직 — 계약조건(청약철회·갱신) 결정론 답 + 준용 NULL 철학."""

from src.v1.rag.terms_sql import is_terms_query, coverage_hint, format_terms


def test_terms_gate_fires_on_terms_queries():
    for q in ["청약철회 언제까지?", "이 특약 갱신되나요?", "만기는 언제?", "청약 철회 며칠 이내?"]:
        assert is_terms_query(q), f"terms 질의 미감지: {q}"


def test_terms_gate_excludes_amount_and_definition():
    for q in ["중환자실 하루 얼마?", "충치는 어떻게 정의되나요?", "보험금 청구 서류는?"]:
        assert not is_terms_query(q), f"비-terms 질의가 terms로 샘: {q}"


def test_coverage_hint_extracts_keyword():
    assert coverage_hint("중환자실 특약 갱신되나요?") == "중환자실"
    assert coverage_hint("청약철회 언제까지?") is None   # 상품 특정 불가


def test_format_terms_deterministic_value():
    """복합약관 — 청약철회 실값 + 갱신 여부."""
    out = format_terms({"is_renewable": True, "cooling_off_days": 15, "resolution_note": None})
    assert "갱신형" in out and "15일 이내" in out


def test_format_terms_includes_cycle_and_term():
    """갱신주기·만기 있으면 노출 — '갱신형(10년 주기) · 10년 만기'."""
    out = format_terms({
        "is_renewable": True, "renewal_cycle_years": 10, "term_years": 10,
        "cooling_off_days": 15, "resolution_note": None,
    })
    assert "10년 주기" in out and "10년 만기" in out and "15일 이내" in out


def test_format_terms_junyong_null_is_honest():
    """특약 청약철회 NULL — 억지 값 대신 '준용 소관 확인 필요'(precision-first)."""
    out = format_terms({
        "is_renewable": True, "cooling_off_days": None,
        "resolution_note": "청약철회 등은 주계약 준용(제19조) 소관. 주계약 미확보 → 답변 불가가 정답.",
    })
    assert "제19조" in out and "준용" in out and "확인 필요" in out
    assert "일 이내" not in out   # 억지 값 안 냄


def test_format_terms_non_renewable():
    out = format_terms({"is_renewable": False, "cooling_off_days": 15})
    assert "비갱신" in out


def test_format_terms_none_returns_rag_signal():
    assert "→RAG" in format_terms(None)
