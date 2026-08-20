"""복합약관 서브계약 감지기(parse_clauses.detect_subcontracts) 순수 로직 잠금.

도메인 불변식 — 특약은 각각 제1조부터 다시 시작(domain-model §2) — 을 이용해 조번호 리셋으로
서브계약을 센다. 단일 약관은 1런, 복합약관은 보통약관 + 특약 N런. 메인 파싱 경로(첫 런만)와
별개인 **검증된 사이드카**라, 회귀 위험 없이 '복합 구조를 정확히 진단'하는 계층의 계약을 못박는다.
"""
from scripts.parse_clauses import detect_subcontracts

_SINGLE = (
    "제1조(목적) 이 약관은 보험계약의 내용을 정합니다.\n"
    "제2조(용어의 정의) 이 약관에서 쓰는 용어의 뜻은 다음과 같습니다.\n"
    "제3조(보험금의 지급) 회사는 보험금을 지급합니다."
)

_COMPOUND = (
    "## 보통약관\n"
    "제1조(계약의 성립) 보험계약은 청약과 승낙으로 이루어집니다.\n"
    "제2조(용어의 정의) 이 약관에서 쓰는 용어의 뜻입니다.\n\n"
    "## 1. 상해입원일당 특별약관\n"
    "제1조(보험금의 지급) 상해로 입원하면 보험금을 지급합니다.\n"
    "제2조(준용규정) 이 특별약관에서 정하지 않은 사항은 보통약관을 따릅니다.\n\n"
    "## 2. 골절진단비 특별약관\n"
    "제1조(보험금의 지급) 골절로 진단되면 보험금을 지급합니다.\n"
    "제2조(준용규정) 이 특별약관에서 정하지 않은 사항은 보통약관을 따릅니다."
)


def test_single_doc_is_one_run():
    """단일 약관은 리셋이 없어 서브계약 1개(전 조 단조증가)."""
    runs = detect_subcontracts(_SINGLE)
    assert len(runs) == 1
    assert runs[0]["first_jo"] == 1 and runs[0]["last_jo"] == 3 and runs[0]["count"] == 3


def test_compound_splits_on_clause_reset():
    """복합약관은 제1조 리셋마다 새 서브계약 = 보통약관 + 특약 2 = 3런."""
    runs = detect_subcontracts(_COMPOUND)
    assert len(runs) == 3
    assert all(r["first_jo"] == 1 and r["last_jo"] == 2 and r["count"] == 2 for r in runs)


def test_compound_runs_carry_heading():
    """각 런에 직전 특약/보통약관 헤딩이 실려 진짜 특약을 식별할 수 있다."""
    runs = detect_subcontracts(_COMPOUND)
    assert runs[0]["heading"].startswith("보통약관")
    assert "상해입원일당 특별약관" in runs[1]["heading"]
    assert "골절진단비 특별약관" in runs[2]["heading"]


def test_no_false_split_within_monotonic_run():
    """번호가 단조증가하는 동안은 쪼개지 않는다(과분할 방지)."""
    runs = detect_subcontracts(
        "제1조(가) 이것은 충분히 긴 본문입니다.\n"
        "제5조(나) 이것도 충분히 긴 본문입니다.\n"
        "제9조(다) 이것 역시 충분히 긴 본문입니다.")
    assert len(runs) == 1 and runs[0]["count"] == 3
