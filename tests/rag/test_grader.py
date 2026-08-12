"""grader.py 단위 테스트.

섹션:
  1. extract_article_refs / extract_appendix_refs / extract_numeric_facts (정규식 추출)
  2. decompose_claims (한국어 종결어미 분해)
  3. _decide_risk (4-level risk 판정 게이트)
  4. verify_answer (오케스트레이션 통합)
  5. Critic dispatch — classify_failure (root cause 5분류) + build_hint
  6. rag 패키지 critic 심볼 export

회귀 시 classify_failure가 extract_*/verify_answer의 silent bug를 상속하는 것 방지.
"""
from __future__ import annotations


# ==============================================================================
# extract_article_refs — 계층형 조항 참조
# ==============================================================================


def test_extract_article_refs_bare():
    from src.v1.rag.grader import extract_article_refs
    refs = extract_article_refs("제43조에 따라")
    assert len(refs) == 1
    assert refs[0].article == 43
    assert refs[0].paragraph is None


def test_extract_article_refs_with_paragraph():
    from src.v1.rag.grader import extract_article_refs
    refs = extract_article_refs("제12조 제3항")
    assert refs[0].article == 12
    assert refs[0].paragraph == 3
    assert refs[0].subparagraph is None


def test_extract_article_refs_full_hierarchy():
    from src.v1.rag.grader import extract_article_refs
    refs = extract_article_refs("제12조 제3항 제1호 제2목")
    r = refs[0]
    assert (r.article, r.paragraph, r.subparagraph, r.item) == (12, 3, 1, 2)


def test_extract_article_refs_multiple_separated():
    from src.v1.rag.grader import extract_article_refs
    refs = extract_article_refs("제1조 내용 ... 제2조 내용 ... 제3조 내용")
    articles = [r.article for r in refs]
    assert set(articles) >= {1, 2, 3}


def test_extract_article_refs_canonical_string():
    from src.v1.rag.grader import extract_article_refs
    refs = extract_article_refs("제12조 제3항 제1호")
    assert refs[0].canonical() == "제12조 제3항 제1호"


def test_extract_article_refs_no_match_returns_empty():
    from src.v1.rag.grader import extract_article_refs
    assert extract_article_refs("조항 언급 없는 일반 문장") == []


# ==============================================================================
# extract_appendix_refs — 별표·부칙·서식·양식
# ==============================================================================


def test_extract_appendix_byeolpyo():
    from src.v1.rag.grader import extract_appendix_refs
    refs = extract_appendix_refs("별표1 참조")
    assert len(refs) == 1
    assert refs[0].kind == "별표"
    assert refs[0].number == 1


def test_extract_appendix_buchick_with_article():
    from src.v1.rag.grader import extract_appendix_refs
    refs = extract_appendix_refs("부칙 제2조")
    assert refs[0].kind == "부칙"
    assert refs[0].number == 2


def test_extract_appendix_seosik():
    from src.v1.rag.grader import extract_appendix_refs
    refs = extract_appendix_refs("서식3 작성")
    assert refs[0].kind == "서식"
    assert refs[0].number == 3


def test_extract_appendix_yangsik():
    from src.v1.rag.grader import extract_appendix_refs
    refs = extract_appendix_refs("양식4 사용")
    assert refs[0].kind == "양식"
    assert refs[0].number == 4


# ==============================================================================
# extract_numeric_facts — 단위 정규화
# ==============================================================================


def test_extract_numeric_manwon_to_krw():
    from src.v1.rag.grader import extract_numeric_facts
    facts = extract_numeric_facts("1,000만원 지급")
    assert len(facts) == 1
    assert facts[0].unit == "KRW"
    assert facts[0].value == 10_000_000


def test_extract_numeric_eokwon_to_krw():
    from src.v1.rag.grader import extract_numeric_facts
    facts = extract_numeric_facts("10억원")
    assert facts[0].unit == "KRW"
    assert facts[0].value == 1_000_000_000


def test_extract_numeric_percent():
    from src.v1.rag.grader import extract_numeric_facts
    facts = extract_numeric_facts("지급률 80%")
    assert facts[0].unit == "PERCENT"
    assert facts[0].value == 80


def test_extract_numeric_gaewol_canonical_equals_days():
    """핵심 단위 정규화: 3개월과 90일이 같은 canonical로 매칭되어야 한다."""
    from src.v1.rag.grader import extract_numeric_facts
    gaewol = extract_numeric_facts("3개월")
    days = extract_numeric_facts("90일")
    assert gaewol[0].canonical() == days[0].canonical()


def test_extract_numeric_date_normalized():
    from src.v1.rag.grader import extract_numeric_facts
    facts = extract_numeric_facts("2026.04.20 이후")
    dates = [f for f in facts if f.unit == "DATE"]
    assert len(dates) == 1
    # 구분자가 '-'로 정규화되어 있어야 다른 표기(/, .)와 비교 가능
    assert "-" in dates[0].original


def test_extract_numeric_no_unit_ignored():
    """단위 없는 raw 숫자는 조항 파싱과 겹쳐 false positive → 제외."""
    from src.v1.rag.grader import extract_numeric_facts
    facts = extract_numeric_facts("약 5 종류")
    # '5'에 단위가 없으니 numeric fact로 잡히지 않아야 함
    assert all(f.unit != "KRW" for f in facts)
    assert all(f.unit != "COUNT" or f.original.strip() != "5" for f in facts)


# ==============================================================================
# decompose_claims — 한국어 종결어미 분리
# ==============================================================================


def test_decompose_claims_period_split():
    from src.v1.rag.grader import decompose_claims
    claims = decompose_claims("첫째 문장이다. 둘째 문장이다.")
    assert len(claims) == 2


def test_decompose_claims_korean_endings():
    from src.v1.rag.grader import decompose_claims
    claims = decompose_claims("지급됩니다. 제외됩니다.")
    assert len(claims) == 2


def test_decompose_claims_newlines():
    from src.v1.rag.grader import decompose_claims
    claims = decompose_claims("첫째 줄\n둘째 줄\n셋째 줄")
    assert len(claims) == 3


def test_decompose_claims_empty_filtered():
    from src.v1.rag.grader import decompose_claims
    claims = decompose_claims("   \n\n  ")
    assert claims == []


# ==============================================================================
# _decide_risk — 4단계 위험 등급
# ==============================================================================


def test_decide_risk_pass_when_all_clean():
    from src.v1.rag.grader import _decide_risk
    assert _decide_risk([], [], []) == "pass"


def test_decide_risk_hard_fail_on_missing_article():
    from src.v1.rag.grader import _decide_risk
    assert _decide_risk(["제99조"], [], []) == "hard_fail"


def test_decide_risk_hard_fail_on_missing_appendix():
    from src.v1.rag.grader import _decide_risk
    assert _decide_risk([], ["별표 5"], []) == "hard_fail"


def test_decide_risk_soft_fail_on_critical_unit():
    from src.v1.rag.grader import _decide_risk, NumericFact
    mismatches = [NumericFact(1_000_000, "KRW", "100만원")]
    assert _decide_risk([], [], mismatches) == "soft_fail"


def test_decide_risk_soft_fail_on_days_mismatch():
    from src.v1.rag.grader import _decide_risk, NumericFact
    mismatches = [NumericFact(90, "DAYS", "90일")]
    assert _decide_risk([], [], mismatches) == "soft_fail"


def test_decide_risk_warn_on_non_critical_unit():
    from src.v1.rag.grader import _decide_risk, NumericFact
    mismatches = [NumericFact(50, "AGE", "50세")]
    assert _decide_risk([], [], mismatches) == "warn"


# ==============================================================================
# verify_answer — 종단 스모크
# ==============================================================================


def test_verify_answer_pass_when_refs_all_match():
    from src.v1.rag.grader import verify_answer
    answer = "제1조에 따라 지급한다."
    context = "제1조 (지급 사유) 피보험자는 보험금을 받는다."
    result = verify_answer(answer, context=context)
    assert result["risk_level"] == "pass"
    assert result["missing_refs"] == []


def test_verify_answer_hard_fail_when_ref_missing():
    from src.v1.rag.grader import verify_answer
    answer = "제99조에 따라 보장된다."
    context = "제1조와 제2조만 존재한다."
    result = verify_answer(answer, context=context)
    assert result["risk_level"] == "hard_fail"
    assert "제99조" in result["missing_refs"]


def test_verify_answer_article_level_match_when_paragraph_only_in_body():
    """답변이 '제10조 제2항'을 인용하고 근거엔 '제10조'(heading)만, 항은 본문에 맨숫자('1'·'2')로
    있을 때 — 조 단위로 매칭돼 grounded(pass)여야 한다. 항 입도로 대조하면 오탐(hard_fail)."""
    from src.v1.rag.grader import verify_answer, Chunk
    chunks = [Chunk(id="863", content="제10조 【보험금 등의 청구】\n1 다음 서류를 제출한다\n2 추가 증빙")]
    answer = "제10조 제2항에 따라 입원간병인 사용확인서를 제출한다."
    result = verify_answer(answer, context="", chunks=chunks)
    assert result["risk_level"] == "pass"
    assert result["missing_refs"] == []
    # citation 매핑도 조 번호로 채워져야 함 (제10조 제2항 → 제10조 담은 863)
    supported = {cid for c in result["claims"] for cid in c["supported_by_chunks"]}
    assert "863" in supported


def test_verify_answer_grounds_ref_from_sibling_context():
    """인용 조가 top-k chunks엔 없고 sibling 확장 context에만 있어도 grounded — LLM이 실제 본
    context가 근거 범위. chunks만 보면 오탐(hard_fail)."""
    from src.v1.rag.grader import verify_answer, Chunk
    chunks = [Chunk(id="863", content="제10조 【청구】 서류를 제출한다")]
    answer = "제4조 제1항의 지급사유에 해당한다."
    # 제4조는 chunks엔 없지만 context(sibling 확장)엔 있음
    result = verify_answer(answer, context="제4조 【보험금 지급사유】 회사는 다음 사유에 보험금을 지급한다", chunks=chunks)
    assert result["risk_level"] == "pass"
    assert result["missing_refs"] == []


def test_verify_answer_soft_fail_on_krw_mismatch():
    """답변이 context에 없는 금액을 언급하면 soft_fail."""
    from src.v1.rag.grader import verify_answer
    answer = "지급 한도는 1,500만원이다."
    context = "제1조 (한도) 지급 한도는 1,000만원이다."
    result = verify_answer(answer, context=context)
    assert result["risk_level"] == "soft_fail"
    # 1,500만원 = 15,000,000원이 mismatch로 잡혀야 함
    mismatch_values = [m["value"] for m in result["numeric_mismatches"]]
    assert 15_000_000 in mismatch_values


# ==============================================================================
# Critic dispatch — classify_failure (root cause 5분류) + build_hint
# (구 test_grader_critic.py 통합)
# ==============================================================================


def test_grader_module_imports():
    from src.v1.rag import grader  # noqa: F401


def test_classify_failure_returns_minor_when_no_issues():
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "pass",
        "missing_refs": [],
        "numeric_mismatches": [],
    }
    assert classify_failure(verification, context="제1조 내용") == "minor"


# ==============================================================================
# generation_error — 인접 조항/항 번호 착각
# ==============================================================================


def test_classify_failure_generation_error_adjacent_article():
    """답변은 제43조 인용, context엔 제42조만 존재 → 인접 조 번호 착각."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "hard_fail",
        "missing_refs": ["제43조"],
        "numeric_mismatches": [],
    }
    context = "제42조(보험금 지급) ... 제42조 제1항에 따라 ..."
    assert classify_failure(verification, context) == "generation_error"


def test_classify_failure_generation_error_adjacent_paragraph():
    """제43조 제2항 인용, context엔 제43조 제1항 — 인접 항 번호 착각."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "hard_fail",
        "missing_refs": ["제43조 제2항"],
        "numeric_mismatches": [],
    }
    context = "제43조 제1항에 따른 내용"
    assert classify_failure(verification, context) == "generation_error"


def test_classify_failure_generation_error_hierarchy_dropped():
    """답변 '제43조'(계층 없음), context '제43조 제1항'(계층 있음) — 한쪽만 계층."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "hard_fail",
        "missing_refs": ["제43조"],
        "numeric_mismatches": [],
    }
    context = "제43조 제1항에 따른 내용"
    assert classify_failure(verification, context) == "generation_error"


# ==============================================================================
# retrieval_gap — missing_ref가 context의 어떤 참조와도 분리
# ==============================================================================


def test_classify_failure_retrieval_gap_far_article():
    """제99조 인용, context엔 제1~5조만 — 번호 거리가 멀면 regenerate 무의미."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "hard_fail",
        "missing_refs": ["제99조"],
        "numeric_mismatches": [],
    }
    context = "제1조 일반사항 ... 제2조 용어 ... 제5조 적용범위 ..."
    assert classify_failure(verification, context) == "retrieval_gap"


def test_classify_failure_retrieval_gap_no_article_in_context():
    """context에 조항 자체가 없음 — 순수 설명문만."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "hard_fail",
        "missing_refs": ["제43조"],
        "numeric_mismatches": [],
    }
    context = "일반적인 안내 문구입니다. 조항 참조 없이 작성된 본문."
    assert classify_failure(verification, context) == "retrieval_gap"


# ==============================================================================
# unit_error — 같은 canonical unit 내 수치 근접 착각
# ==============================================================================


def test_classify_failure_unit_error_krw_proximity():
    """답변 '1,200만원', context '1,000만원' — 같은 KRW, 20% 차이 → 수치 착각."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "soft_fail",
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 12_000_000.0, "unit": "KRW", "original": "1,200만원"},
        ],
    }
    context = "보험금은 1,000만원을 한도로 한다."
    assert classify_failure(verification, context) == "unit_error"


def test_classify_failure_unit_error_days_proximity():
    """답변 '100일', context '90일' — 같은 DAYS, 11% 차이."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "soft_fail",
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 100.0, "unit": "DAYS", "original": "100일"},
        ],
    }
    context = "최대 90일까지 보장한다."
    assert classify_failure(verification, context) == "unit_error"


def test_classify_failure_unit_error_far_value_falls_through():
    """답변 '5억원', context '100만원' — 같은 KRW지만 차이가 너무 큼 (>=50%) → unit_error 아님."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "soft_fail",
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 500_000_000.0, "unit": "KRW", "original": "5억원"},
        ],
    }
    context = "수수료 100만원이 부과된다."
    # 5억 vs 100만: |500_000_000 - 1_000_000| / 500_000_000 = 0.998 → proximity 0.5 이상이라 unit_error 아님
    # 비크리티컬 단위는 minor로 분기 — 여기는 unit_error 아님만 검증.
    assert classify_failure(verification, context) != "unit_error"


# ==============================================================================
# minor — 비즈니스 비크리티컬 단위(AGE/COUNT)는 unit_error에서 제외
# ==============================================================================


def test_classify_failure_minor_age_mismatch():
    """답변 '50세', context '48세' — AGE는 비즈니스 비크리티컬 → regenerate 무가치, minor."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "warn",
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 50.0, "unit": "AGE", "original": "50세"},
        ],
    }
    context = "피보험자 연령 48세 이상."
    # 50 vs 48: 4% 차이라 proximity는 통과하지만, AGE는 critical 아니라 unit_error 아님
    assert classify_failure(verification, context) == "minor"


def test_classify_failure_minor_count_mismatch():
    """답변 '5회', context '4회' — COUNT도 비크리티컬."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "warn",
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 5.0, "unit": "COUNT", "original": "5회"},
        ],
    }
    context = "최대 4회까지 청구 가능."
    assert classify_failure(verification, context) == "minor"


def test_classify_failure_unit_error_when_mixed_critical_and_minor():
    """KRW(critical) + AGE(non-critical) 혼합 — critical 하나라도 근접하면 unit_error."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "soft_fail",
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 50.0, "unit": "AGE", "original": "50세"},  # 비크리티컬
            {"value": 12_000_000.0, "unit": "KRW", "original": "1,200만원"},  # 크리티컬
        ],
    }
    context = "60세 이하 가입자에 한해 1,000만원을 한도로 한다."
    # KRW가 critical이고 1,000만원과 근접 → unit_error 우선
    assert classify_failure(verification, context) == "unit_error"


# ==============================================================================
# semantic_mismatch — 외부 LLM judge 주입 슬롯
# ==============================================================================


def test_classify_failure_semantic_mismatch_via_injected_judge():
    """regex 검증은 통과(pass)하지만 의미가 반대인 경우 — judge가 잡아야."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "pass",  # 구조적으로는 문제 없음
        "missing_refs": [],
        "numeric_mismatches": [],
    }
    context = "이 경우 보장하지 아니합니다."
    answer = "보장됩니다."

    # 가짜 judge: 답변과 context가 의미상 반대인지 단순 토큰으로 판정
    def mock_judge(a: str, c: str) -> bool:
        return "보장됩니다" in a and "아니합니다" in c

    result = classify_failure(
        verification, context,
        answer=answer, semantic_judge=mock_judge,
    )
    assert result == "semantic_mismatch"


def test_classify_failure_semantic_judge_returns_false_falls_through():
    """judge가 False (반전 아님)면 minor."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "pass",
        "missing_refs": [],
        "numeric_mismatches": [],
    }
    context = "보장됩니다."
    answer = "보장됩니다."

    result = classify_failure(
        verification, context,
        answer=answer, semantic_judge=lambda a, c: False,
    )
    assert result == "minor"


def test_classify_failure_no_judge_skips_semantic_check():
    """judge 미주입(기본값) — 의미 검증 비활성, minor 반환."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "pass",
        "missing_refs": [],
        "numeric_mismatches": [],
    }
    # answer는 줘도 judge 없으면 의미 검증 안 됨
    result = classify_failure(verification, context="...", answer="반대 의미")
    assert result == "minor"


def test_classify_failure_judge_no_answer_skips_semantic_check():
    """judge는 있는데 answer 없으면 의미 검증 안 됨 (안전 fallback)."""
    from src.v1.rag.grader import classify_failure

    verification = {
        "risk_level": "pass",
        "missing_refs": [],
        "numeric_mismatches": [],
    }
    result = classify_failure(
        verification, context="...",
        semantic_judge=lambda a, c: True,  # 항상 True여도
        # answer 없음
    )
    assert result == "minor"  # answer 없으니 judge 호출 안 됨


# ==============================================================================
# build_hint — generation_error: 허용 참조 + 금지 참조 텍스트 생성
# ==============================================================================


def test_build_hint_generation_error_includes_allowed_refs():
    """context에 등장하는 모든 조항이 hint의 '허용 참조' 섹션에 들어가야 함."""
    from src.v1.rag.grader import build_hint

    verification = {"missing_refs": ["제43조"], "numeric_mismatches": []}
    context = "제41조 일반사항 ... 제42조 적용범위 ... 제44조 효력 ..."
    hint = build_hint("generation_error", verification, context)
    assert "제41조" in hint
    assert "제42조" in hint
    assert "제44조" in hint


def test_build_hint_generation_error_includes_forbidden_missing_ref():
    """이전 답변의 missing_ref(잘못 인용한 번호)가 '사용 금지' 섹션에 들어가야 함."""
    from src.v1.rag.grader import build_hint

    verification = {"missing_refs": ["제43조"], "numeric_mismatches": []}
    context = "제41조 ... 제42조 ... 제44조 ..."
    hint = build_hint("generation_error", verification, context)
    assert "제43조" in hint
    # "사용 금지" 또는 비슷한 키워드가 들어가야 LLM이 명확히 인식
    assert "금지" in hint or "사용하지" in hint


def test_build_hint_generation_error_appendix_included():
    """별표·부칙 같은 appendix 참조도 허용 목록에 포함."""
    from src.v1.rag.grader import build_hint

    verification = {"missing_refs": ["제43조"], "numeric_mismatches": []}
    context = "제1조 ... 별표1 ... 부칙 제2조 ..."
    hint = build_hint("generation_error", verification, context)
    assert "제1조" in hint
    assert "별표 1" in hint
    assert "부칙 제2조" in hint


def test_build_hint_minor_returns_empty_string():
    """minor는 regenerate 안 하므로 hint 생성 불필요 → 빈 문자열."""
    from src.v1.rag.grader import build_hint

    hint = build_hint("minor", {"missing_refs": [], "numeric_mismatches": []}, "")
    assert hint == ""


def test_build_hint_retrieval_gap_returns_empty_string():
    """retrieval_gap은 regenerate 금지 (escalate) → hint 불필요."""
    from src.v1.rag.grader import build_hint

    hint = build_hint("retrieval_gap", {"missing_refs": ["제99조"], "numeric_mismatches": []}, "...")
    assert hint == ""


# ==============================================================================
# build_hint — unit_error: 허용 수치 + 금지 수치 텍스트
# ==============================================================================


def test_build_hint_unit_error_includes_allowed_numerics():
    """context 수치(original 형태)가 hint의 '허용 수치' 섹션에 포함되어야 함."""
    from src.v1.rag.grader import build_hint

    verification = {
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 12_000_000.0, "unit": "KRW", "original": "1,200만원"},
        ],
    }
    context = "보험금은 1,000만원을 한도로 하며, 90일까지 보장한다."
    hint = build_hint("unit_error", verification, context)
    assert "1,000만원" in hint
    assert "90일" in hint


def test_build_hint_unit_error_includes_forbidden_mismatch():
    """이전 답변의 잘못된 수치(original)가 '사용 금지' 섹션에 들어가야 함."""
    from src.v1.rag.grader import build_hint

    verification = {
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 12_000_000.0, "unit": "KRW", "original": "1,200만원"},
        ],
    }
    context = "보험금은 1,000만원을 한도로 한다."
    hint = build_hint("unit_error", verification, context)
    assert "1,200만원" in hint
    assert "금지" in hint or "사용하지" in hint


def test_build_hint_unit_error_multiple_mismatches():
    """여러 수치 mismatch가 있을 때 모두 금지 목록에 포함."""
    from src.v1.rag.grader import build_hint

    verification = {
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 12_000_000.0, "unit": "KRW", "original": "1,200만원"},
            {"value": 100.0, "unit": "DAYS", "original": "100일"},
        ],
    }
    context = "1,000만원 한도, 90일 보장."
    hint = build_hint("unit_error", verification, context)
    assert "1,200만원" in hint
    assert "100일" in hint


def test_build_hint_unit_error_with_no_context_numerics():
    """context에 수치가 없어도 금지 목록은 여전히 LLM에 전달 (최소 guardrail)."""
    from src.v1.rag.grader import build_hint

    verification = {
        "missing_refs": [],
        "numeric_mismatches": [
            {"value": 12_000_000.0, "unit": "KRW", "original": "1,200만원"},
        ],
    }
    context = "일반 안내 문구, 수치 언급 없음."
    hint = build_hint("unit_error", verification, context)
    # 허용 수치 섹션은 비어있어도, 금지 수치는 있어야
    assert "1,200만원" in hint


# ==============================================================================
# REGENERATE_WITH_HINT_PROMPT — hint를 주입할 LLM 프롬프트 템플릿
# ==============================================================================


def test_regenerate_prompt_formats_with_hint():
    """프롬프트가 context/query/hint 3개 변수를 받아 system+human 메시지 2개로 렌더링."""
    from src.v1.rag.prompts import REGENERATE_WITH_HINT_PROMPT

    messages = REGENERATE_WITH_HINT_PROMPT.format_messages(
        context="제1조 ... 제2조 ...",
        query="보험금 지급 조건은?",
        hint="허용 참조: 제1조, 제2조\n사용 금지: 제99조",
    )
    assert len(messages) == 2
    system_text = messages[0].content
    # context, hint 모두 system 프롬프트에 들어가야 함
    assert "제1조 ... 제2조 ..." in system_text
    assert "허용 참조: 제1조, 제2조" in system_text
    assert "사용 금지: 제99조" in system_text
    # query는 human 메시지로
    assert messages[1].content == "보험금 지급 조건은?"


def test_regenerate_prompt_distinguishes_critic_feedback_from_general_rules():
    """LLM이 hint를 일반 규칙과 구분해서 우선순위 높게 인식하도록 명시 필요."""
    from src.v1.rag.prompts import REGENERATE_WITH_HINT_PROMPT

    messages = REGENERATE_WITH_HINT_PROMPT.format_messages(
        context="...",
        query="...",
        hint="허용 참조: 제1조",
    )
    system_text = messages[0].content
    # critic 피드백임을 명시하는 키워드 (LLM이 강제 사항으로 인식해야)
    assert "Critic" in system_text or "critic" in system_text or "피드백" in system_text or "준수" in system_text


# ==============================================================================
# rag 패키지 public API — router에서 import하는 심볼 export 검증
# ==============================================================================


def test_rag_package_exports_critic_symbols():
    """rag 패키지에서 classify_failure, build_hint, FailureType, REGENERATE_WITH_HINT_PROMPT 직접 import 가능."""
    from src.v1 import rag
    assert hasattr(rag, "classify_failure")
    assert hasattr(rag, "build_hint")
    assert hasattr(rag, "FailureType")
    assert hasattr(rag, "REGENERATE_WITH_HINT_PROMPT")


def test_rag_package_star_import_includes_critic_symbols():
    """__all__에 신규 심볼이 들어가 있어야 `from src.v1.rag import *`에서도 접근 가능."""
    from src.v1.rag import __all__
    assert "classify_failure" in __all__
    assert "build_hint" in __all__
    assert "FailureType" in __all__
    assert "REGENERATE_WITH_HINT_PROMPT" in __all__
