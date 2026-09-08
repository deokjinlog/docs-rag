"""공통 블록 스키마 + 읽기순서 재구성 (캐스케이드 M2).

핵심은 **전폭 요소**다. 기존 구현은 단을 x0 로만 갈라서, 두 단을 가로지르는 제목이
"왼쪽 단"으로 분류돼 본문 사이에 끼었다 — 실측(2026-09-07): 제목 bbox x0=176·x1=424 가
단 경계 ~300 을 가로지르는데 x0<300 이라 0번 단으로 갔다.
전폭은 어느 단도 아니므로 **밴드 경계**로 써야 제자리에 온다.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from blocks import Block, column_index, page_column_bounds, reorder, to_markdown  # noqa: E402


def _b(x0, y, text, x1=None, page=1):
    """PDF 좌표(y 위로 증가). y 가 클수록 페이지 위쪽.

    정렬은 **y0(아래 모서리)** 를 쓴다 — 기존 구현(bb[1])과 맞추기 위해서다.
    그래서 여기서 y 를 y0 로 넣는다."""
    return Block(page=page, bbox=(x0, y, x1 if x1 is not None else x0 + 100, y + 10), text=text)


def test_두_단이_감지된다():
    blocks = [_b(50, 700, "L1"), _b(50, 600, "L2"), _b(310, 700, "R1"), _b(310, 600, "R2")]
    assert len(page_column_bounds(blocks)) == 1


def test_전폭_요소를_판별한다():
    """제목은 실제 문서처럼 좌측 여백에서 시작해 페이지를 가로지른다.

    ⚠ **경계 검출의 한계**: 제목 x0 가 두 단 사이에 홀로 있으면(예: 가운데 정렬로
    x0=176) 그 x0 자체가 가짜 경계를 만들어 좌단까지 '전폭'으로 잡힌다. 군집 크기로
    거르는 수정을 시도했으나 실제 문서에서 오름차순 비율이 떨어져 되돌렸다
    (blocks.reorder 주석). spans_columns 를 정렬에 쓰려면 이 한계부터 풀어야 한다."""
    blocks = [_b(50, 700, "L1"), _b(50, 600, "L2"), _b(310, 700, "R1"), _b(310, 600, "R2"),
              _b(50, 800, "TITLE", x1=550)]
    from blocks import spans_columns
    bounds = page_column_bounds(blocks)
    title = [b for b in blocks if b.text == "TITLE"][0]
    assert spans_columns(title, bounds), "경계를 가로지르면 전폭"
    assert not spans_columns(blocks[0], bounds)


def test_제목이_맨_앞에_온다():
    """전폭은 0번 단으로 들어가고 y 가 가장 위라 결과적으로 맨 앞에 온다."""
    blocks = [_b(50, 700, "L1"), _b(50, 600, "L2"), _b(310, 700, "R1"), _b(310, 600, "R2"),
              _b(50, 800, "TITLE", x1=550)]
    assert [b.text for b in reorder(blocks)] == ["TITLE", "L1", "L2", "R1", "R2"]


def test_전폭_판별은_되지만_정렬엔_안_쓴다():
    """**알려진 한계**를 잠근다 — 밴드 분할을 시도했다가 되돌렸기 때문이다.

    정답은 [L상, L상2, R상, R상2, TABLE, L하, R하] 인데 현재는 전폭이 0번 단에 들어가
    좌단 흐름에 섞인다. 밴드로 고치려 했으나 실제 문서에서 오름차순 비율이 전부
    떨어졌다(전폭 요소가 페이지당 p50 16개라 밴드가 잘게 쪼개짐, blocks.reorder 주석).
    이 테스트는 "고쳐졌다고 착각하지 않기" 위해 현재 동작을 명시해 둔다."""
    blocks = [
        _b(50, 700, "L상"), _b(50, 650, "L상2"), _b(310, 700, "R상"), _b(310, 650, "R상2"),
        _b(60, 500, "TABLE", x1=540),
        _b(50, 400, "L하"), _b(310, 400, "R하"),
    ]
    from blocks import spans_columns
    bounds = page_column_bounds(blocks)
    table = [b for b in blocks if b.text == "TABLE"][0]
    assert spans_columns(table, bounds), "전폭 판별 자체는 된다"
    got = [b.text for b in reorder(blocks)]
    assert got != ["L상", "L상2", "R상", "R상2", "TABLE", "L하", "R하"], \
        "정렬에 쓰기 시작했으면 blocks.reorder 주석의 실측 회귀도 재확인할 것"


def test_단일_단은_y_순서만():
    blocks = [_b(50, 500, "b"), _b(50, 700, "a"), _b(50, 300, "c")]
    assert [b.text for b in reorder(blocks)] == ["a", "b", "c"]


def test_페이지_순서가_먼저다():
    blocks = [_b(50, 700, "p2", page=2), _b(50, 700, "p1", page=1)]
    assert [b.text for b in reorder(blocks)] == ["p1", "p2"]


def test_order_가_채워진다():
    out = reorder([_b(50, 700, "a"), _b(50, 600, "b")])
    assert [b.order for b in out] == [0, 1]


def test_markdown_은_heading_level_을_쓴다():
    b1, b2 = _b(50, 700, "제목"), _b(50, 600, "본문")
    b1.heading_level = 2
    assert to_markdown([b1, b2]) == "## 제목\n본문"


def test_OCR_어댑터가_confidence_를_싣는다():
    """M5 의 줄 단위 격상이 이 값으로 돈다 — 안 실으면 격상 설계가 불가능해진다."""
    from blocks import from_ocr
    res = {"overall_ocr_res": {
        "rec_texts": ["법랑질(Enamel)", "움"],
        "rec_scores": [0.966, 0.117],
        "dt_polys": [[[10, 10], [90, 10], [90, 30], [10, 30]],
                     [[10, 40], [50, 40], [50, 60], [10, 60]]]}}
    bs = from_ocr(res, page=3)
    assert len(bs) == 2
    assert bs[0].confidence == 0.966 and bs[1].confidence == 0.117
    assert bs[0].source == "ocr" and bs[0].page == 3
    assert bs[0].bbox == (10.0, 10.0, 90.0, 30.0)


def test_빈_텍스트는_버린다():
    from blocks import from_ocr
    res = {"overall_ocr_res": {"rec_texts": ["", "  ", "실제"], "rec_scores": [0, 0, 0.9]}}
    assert [b.text for b in from_ocr(res)] == ["실제"]
