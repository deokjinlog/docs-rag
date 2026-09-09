"""페이지 triage 규칙 잠금.

여기서 잠그는 건 "함수가 도나"가 아니라 **실측으로 정한 판단들**이다. 각 테스트는 그것을
정하게 만든 숫자를 주석에 달고 있다 — 나중에 누가 바꾸려 할 때 근거를 다시 재게 하려고.
"""
import pytest

from src.v1.utils.triage import (
    PageSignals, anchor_hits, classify, difficulty_tags,
    garbage_ratio, load_config, separator_ratio,
)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _sig(**kw):
    base = dict(page=1, page_w=595.0, page_h=842.0)
    base.update(kw)
    return PageSignals(**base)


# ── 구분자 제어문자 ──────────────────────────────────────────────────────────
def test_separator_control_chars_are_not_garbage():
    """\\x01~\\x08 은 손상이 아니라 **단어 구분자 대용**이다.

    실측(finished 12문서·86p 표본): 이 구간 문자가 38% 페이지에 있고 비율 중앙값 0.157 로
    임계 0.15 를 넘겨 코퍼스의 1/4 이 NATIVE_SUSPECT 로 오분류됐다. 진짜 손상 마커인
    U+FFFD 는 같은 표본에서 0건. 이걸 되돌리면 그 오분류가 그대로 돌아온다.
    """
    text = "항문질환\x01치료\x01및\x01한의원\x01치료는\x01보상이\x01안되나요"
    assert garbage_ratio(text) == 0.0
    assert separator_ratio(text) > 0.05


def test_real_corruption_is_garbage():
    """U+FFFD·분리 자모는 계속 잡는다 — 구분자를 뺐다고 손상까지 놓치면 안 된다."""
    assert garbage_ratio("보험금���지급") > 0.15
    assert garbage_ratio("보험금") > 0.15


# ── 3-값 판정 ────────────────────────────────────────────────────────────────
def test_classify_is_three_valued(cfg):
    """SCAN 을 SIMPLE/COMPLEX 로 가르지 않는다 — PP-StructureV3 가 레이아웃을 포함하므로
    스캔 페이지를 미리 둘로 나눌 이유가 없다. VL 은 OCR 결과를 보고 하는 격상이다."""
    assert classify(_sig(char_count=500), cfg) == "NATIVE"
    assert classify(_sig(char_count=500, garbage_ratio=0.3), cfg) == "NATIVE_SUSPECT"
    assert classify(_sig(char_count=10), cfg) == "SCAN"
    # 표가 많든 다단이든 스캔은 그냥 SCAN 이다
    assert classify(_sig(char_count=10, table_count=5,
                         left_blocks=9, right_blocks=9), cfg) == "SCAN"


def test_garbage_is_checked_before_char_count(cfg):
    """순서가 곧 로직이다. char_count 로 먼저 통과시키면 깨진 텍스트가 NATIVE 로 샌다."""
    assert classify(_sig(char_count=5000, garbage_ratio=0.9), cfg) == "NATIVE_SUSPECT"


def test_font_flag_alone_does_not_suspect(cfg):
    """폰트 플래그 단독으로는 의심하지 않는다.

    실측: finished 12문서에서 no_tounicode 0건. 유일하게 걸린 문서(관악구 명세서)는
    25p 전량이 걸렸지만 추출문이 육안으로 정확했다(종전지번·호수·블록,롯트…).
    단독 트리거로 두면 멀쩡한 문서를 통째로 비싼 ODL-vs-OCR 경로로 보낸다.
    """
    assert classify(_sig(char_count=500, font_suspect=False), cfg) == "NATIVE"
    assert classify(_sig(char_count=500, font_suspect=True), cfg) == "NATIVE_SUSPECT"


# ── 도메인 앵커 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "제5조(보험금의 지급사유)", "제 12 조", "① 회사는", "가. 사망보험금", "1. 일반상해",
])
def test_anchor_matches_clause_markers(text):
    """앵커가 0건인데 글자 수는 정상 = '글자는 읽었는데 구조가 안 보인다'.
    conf 가 높아도 오인식일 수 있는 경우(제l2조)를 잡는 유일한 신호라 정확해야 한다."""
    assert anchor_hits(text) >= 1


def test_anchor_absent_in_plain_prose():
    assert anchor_hits("이 문서는 아무런 조문 표지가 없는 평범한 문장입니다") == 0


# ── 다단 판정 ────────────────────────────────────────────────────────────────
def test_multi_column_needs_blocks_on_both_sides(cfg):
    """홈통만으로는 부족하다 — 판별을 실제로 하는 건 '양쪽 각 3블록' 쪽이다.
    x0 클러스터링으로 세면 중첩 목록 들여쓰기가 단으로 잡혀 41.5% 가 다단이 됐다(실측)."""
    wide_gap_one_side = _sig(gutter_pt=40, gutter_x=300, left_blocks=9, right_blocks=0)
    assert "multi_column" not in difficulty_tags(wide_gap_one_side, cfg)
    real_two_col = _sig(gutter_pt=40, gutter_x=300, left_blocks=9, right_blocks=8)
    assert "multi_column" in difficulty_tags(real_two_col, cfg)


def test_table_dense_cell_rule_is_engine_conditional(cfg):
    """ODL 은 표 보유 페이지의 셀 중앙값이 6개(2x3)로 과소 추출이라 셀 기준을 못 쓴다.
    같은 페이지를 PyMuPDF 로 재면 1,380개였다."""
    s = _sig(table_area_ratio=0.05, cell_count=60)
    assert "table_dense" not in difficulty_tags(s, cfg, engine="odl")
    assert "table_dense" in difficulty_tags(s, cfg, engine="ocr")
