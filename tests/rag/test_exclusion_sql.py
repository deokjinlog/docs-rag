"""SQL 경로 exclusion_sql 순수 로직 — 면책 상세("뭐가 면책?")."""

from src.v1.rag.exclusion_sql import (
    is_exclusion_query, format_exclusions, common_exclusion_note,
)


def test_exclusion_gate_fires():
    for q in ["뭐가 면책인가요?", "지급 안 되는 사유는?", "보장 안 되는 경우는?", "면책사항 알려줘"]:
        assert is_exclusion_query(q), f"면책 질의 미감지: {q}"


def test_exclusion_gate_excludes_others():
    for q in ["중환자실 하루 얼마?", "청약철회 언제까지?", "충치는 어떻게 정의되나요?"]:
        assert not is_exclusion_query(q), f"비-면책 질의가 샘: {q}"


def test_format_exclusions_lists_reason_tags():
    """body에서 표준 사유 태그 나열 + 조 참조."""
    out = format_exclusions([
        {"jo": 7, "title": "보험금을 지급하지 않는 사유",
         "body": "1. 고의로 자신을 해친 경우 2. 전쟁, 내란으로"},
    ])
    assert "고의" in out and "전쟁내란" in out and "제7조" in out


def test_format_exclusions_empty_is_rag():
    assert "→RAG" in format_exclusions([])


def test_format_exclusions_no_tags_falls_back_to_refs():
    """태그 못 뽑으면 조 참조만."""
    out = format_exclusions([{"jo": 5, "title": "면책", "body": "특이 사유 없음"}])
    assert "제5조" in out


# ── 준용 완결성 — 특약 공통면책 미확보를 정직하게 명시 ──────────────────────
def test_common_exclusion_note_when_junyong():
    """resolution_note에 준용 있으면 공통면책 준용 소관 명시(조 번호 뽑음)."""
    note = common_exclusion_note("청약철회 등은 주계약 준용(제19조) 소관. 주계약 미확보.")
    assert "공통면책" in note and "제19조" in note and "확인 필요" in note


def test_common_exclusion_note_empty_when_not_junyong():
    """준용 아니면(복합약관 자체 완비) 노트 없음."""
    assert common_exclusion_note(None) == ""
    assert common_exclusion_note("자체 면책 완비") == ""


def test_format_exclusions_tukyak_appends_junyong_completeness():
    """특약 면책 답 = 고유 사유 + 공통면책 준용 미확보(완결·정직)."""
    out = format_exclusions(
        [{"jo": 7, "title": "면책", "body": "1. 고의로 자신을 해친 경우"}],
        resolution_note="주계약 준용(제19조) 소관, 미확보",
    )
    assert "고의" in out and "공통면책" in out and "준용" in out


def test_format_exclusions_junyong_only_when_no_self_clause():
    """특약이 자체 면책 조가 없어도 준용 공통면책은 안내(완전 침묵 방지)."""
    out = format_exclusions([], resolution_note="주계약 준용(제19조) 소관")
    assert "공통면책" in out and "→RAG" not in out
