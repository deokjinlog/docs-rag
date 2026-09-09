"""VL 출력 온전성 게이트 잠금 (캐스케이드 M5).

핵심 계약 두 개를 못박는다:
  ① **마크업은 반복 통계에 안 잡힌다** — VL 은 표를 HTML 로 뱉고 `word-wrap` 같은 속성이
     셀마다 반복된다. 이걸 세면 멀쩡한 표 페이지가 루프로 오탐된다(실측 0.646).
  ② **루프는 잡는다** — 실측 실패는 8-gram 하나가 556회 반복이었다.

비대칭도 함께 잠근다: 오탐은 그 페이지가 OCR 품질로 내려앉을 뿐이지만, 미탐은 같은 구절이
수백 번 박힌 청크를 색인해 확신에 찬 오답의 재료가 된다.
"""
import pytest

from scripts.vl_sanity import accept_vl, repetition_stats, visible_text


def test_markup_is_stripped_before_counting():
    """반복되는 건 style 속성뿐인 정상 표 → 통과해야 한다."""
    cell = "<td style='text-align: center; word-wrap: break-word;'>{}</td>"
    rows = "".join(cell.format(f"제{i}조 보험금의 지급사유 항목 {i}번") for i in range(1, 30))
    md = f"<table border=1 style='margin: auto; word-wrap: break-word;'><tr>{rows}</tr></table>"
    ok, reason, st = accept_vl(md)
    assert ok, reason
    # 가시 텍스트에 태그 흔적이 남으면 안 된다
    assert "word-wrap" not in visible_text(md)
    assert "<td" not in visible_text(md)


def test_decoding_loop_is_rejected():
    """실측 실패 모드 재현 — 한 구절이 수백 번."""
    md = "0원, 100만원, 200만원, 300만원, 400만원, 500만원 " * 100
    ok, reason, st = accept_vl(md)
    assert not ok
    assert "루프" in reason
    assert st["max_ngram_count"] >= 30


def test_normal_prose_passes():
    body = (
        "회사는 피보험자가 보험기간 중 상해의 직접결과로써 사망한 경우 보험수익자에게 "
        "약정한 보험가입금액을 사망보험금으로 지급합니다. 다만 계약자 또는 피보험자의 "
        "고의로 인한 경우에는 보험금을 지급하지 않습니다. 청약철회는 청약일부터 15일 "
        "이내에 가능하며 이미 납입한 보험료를 돌려드립니다."
    )
    ok, reason, _ = accept_vl(body)
    assert ok, reason


def test_short_output_is_not_judged():
    """통계가 요동치는 짧은 출력은 판정 대상이 아니다 — 빈 결과는 상위 격상 로직 소관."""
    ok, reason, st = accept_vl("가나다" * 10)
    assert ok and reason is None
    assert st["visible_chars"] < 200


@pytest.mark.parametrize("text,expect_rep", [("가나다라마바사아자", 0.0), ("가나다라마바사아" * 50, 0.98)])
def test_self_repeat_direction(text, expect_rep):
    """자기반복도는 0(전부 고유)~1(전부 중복) 방향이 뒤집히지 않는다."""
    assert repetition_stats(text)["self_repeat"] == pytest.approx(expect_rep, abs=0.03)
