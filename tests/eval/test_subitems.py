"""항/호/목 세분 파서(parse_clauses.parse_subitems) 순수 로직.

조 본문을 항(①)→호(1.)→목(가.) 계층으로 세분한다(domain-model §3, 정밀 인용용).
핵심 계약: 마커는 **순차**일 때만 인정 → 본문 중간 숫자·글자 오인식 방지(precision-first).
"""
from scripts.parse_clauses import parse_subitems, subitem_counts


def test_sequential_hangs():
    text = "- ① 첫째 항.\n- ② 둘째 항.\n- ③ 셋째 항."
    h = parse_subitems(text)
    assert subitem_counts(h) == (3, 0, 0)
    assert [x["hang"] for x in h] == [1, 2, 3]


def test_hangless_ho_uses_hang0_container():
    """항 없이 호로 가는 조(면책형) — hang=0 컨테이너에 호를 담고, 항 수는 0."""
    text = "회사는 다음의 경우 지급하지 않습니다.\n- 1. 고의.\n- 2. 전쟁.\n- 3. 위험활동."
    h = parse_subitems(text)
    assert subitem_counts(h) == (0, 3, 0)          # 항 0, 호 3
    assert h[0]["hang"] == 0 and len(h[0]["hos"]) == 3


def test_nested_hang_ho_mok():
    text = ("- ① 다음 각 호.\n- 1. 첫 호.\n- 가. 첫 목.\n- 나. 둘 목.\n"
            "- 2. 둘 호.\n- ② 둘째 항.")
    h = parse_subitems(text)
    nh, nho, nmok = subitem_counts(h)
    assert (nh, nho, nmok) == (2, 2, 2)


def test_non_sequential_marker_rejected():
    """②를 건너뛴 ③은 마커로 안 봄(본문 연속으로 흡수) — 오탐 방지."""
    text = "- ① 첫째.\n- ③ 이건 마커 아님(순서 안 맞음)."
    h = parse_subitems(text)
    assert subitem_counts(h)[0] == 1               # 항은 ① 하나만
    assert "③" in h[0]["text"]                     # ③ 줄은 ①의 본문으로 흡수


def test_spurious_ho_not_starting_at_one_rejected():
    """1.로 시작하지 않는 '5.'는 호 아님(연속 흡수)."""
    text = "본문입니다.\n- 5. 이건 호 아님."
    h = parse_subitems(text)
    assert subitem_counts(h)[1] == 0


def test_single_paragraph_has_no_subitems():
    text = "회사는 피보험자에게 입원급여금을 지급합니다. (별표1 참조)"
    assert subitem_counts(parse_subitems(text)) == (0, 0, 0)


def test_continuation_lines_appended():
    """마커 없는 줄은 가장 깊은 현재 노드에 이어붙는다."""
    text = "- ① 첫 항 시작\n이어지는 설명.\n- ② 둘째 항."
    h = parse_subitems(text)
    assert h[0]["hang"] == 1 and "이어지는 설명" in h[0]["text"]


def test_markdown_dash_prefix_tolerated():
    """ODL의 '- ' 리스트 접두가 붙어도 마커 인식."""
    a = parse_subitems("- ① 항.\n- 1. 호.")
    b = parse_subitems("① 항.\n1. 호.")
    assert subitem_counts(a) == subitem_counts(b) == (1, 1, 0)
