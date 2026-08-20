"""반각 프로파일에서 마크다운 헤딩 조(## 제N조(제목)) 인식 — parse_clauses 회귀 잠금.

일부 약관(회사미상 상해질병·수술비 등)은 조 제목을 `## 제3조(계약의 무효)`처럼 **마크다운
헤딩**으로 뽑고 본문을 다음 줄에 둔다. 반각 프로파일의 목차 배제(괄호 뒤 본문 없음 → 스킵)가
이 진짜 조를 목차로 오인해 통째로 누락하던 회귀를, `#` 헤딩 마커 면제로 복원했다(precision-first).
목차·인라인 참조엔 `#`가 없으므로 여전히 배제된다 — 이 두 성질을 동시에 못박는다.
"""
from scripts.parse_clauses import parse_clauses, select_profile

# 반각() 제목만 → half 프로파일. 조 3(헤딩·다음줄 본문) / 5(인라인 본문) /
# 7(맨줄 목차형: # 없음·본문 없음 → 배제돼야) / 9(헤딩).
_MD = """# 어떤 상해보험 특별약관

## 제3조(계약의 무효)
회사는 다음 중 어느 하나에 해당하는 계약을 무효로 합니다.

제5조(계약자의 임의해지) 계약자는 계약이 소멸하기 전에는 언제든지 계약을 해지할 수 있습니다.

제7조(계약의 소멸)

## 제9조(준용규정)
이 특별약관에서 정하지 아니한 사항은 보통약관을 따릅니다.
"""


def _parsed():
    return parse_clauses(_MD, "TEST_HALF_2024")


def test_profile_is_half():
    """전각【】가 없으니 반각(half) 프로파일이어야 이 경로를 탄다."""
    assert select_profile(_MD) == "half"


def test_markdown_heading_clauses_recovered():
    """## 제N조(제목)은 본문이 다음 줄에 있어도 진짜 조로 잡힌다(과거엔 목차로 오인·누락)."""
    jos = {c["jo"] for c in _parsed()}
    assert 3 in jos and 9 in jos


def test_inline_body_clause_still_caught():
    """줄 안에 본문이 붙은 반각 조(제5조 … 본문)도 그대로 잡힌다."""
    assert 5 in {c["jo"] for c in _parsed()}


def test_bare_toc_line_still_excluded():
    """# 헤딩·인라인 본문 둘 다 없는 맨줄(제7조(계약의 소멸))은 목차로 배제 — precision 유지."""
    assert 7 not in {c["jo"] for c in _parsed()}


def test_recovered_titles_are_correct():
    """복원된 헤딩 조의 제목이 괄호 안 그대로 파싱된다."""
    by_jo = {c["jo"]: c["title"] for c in _parsed()}
    assert by_jo[3] == "계약의 무효"
    assert by_jo[9] == "준용규정"
