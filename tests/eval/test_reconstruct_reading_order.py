"""다단 읽기순서 교정(reconstruct_reading_order) 순수 로직 잠금.

ODL이 2단 PDF를 가로질러 읽어 뒤섞는 것을 page+bbox로 복원한다. 핵심 계약: PDF 좌표는 y가
위로 증가 → y 내림차순이 위→아래이고, 단은 왼쪽 먼저(x0 갭으로 경계 검출). 이 두 성질을 못박는다.
"""
from scripts.reconstruct_reading_order import (
    column_index, page_column_bounds, reorder, flatten_nodes,
)


def _n(pg, x0, y0, content):
    return {"pg": pg, "bb": [x0, y0, x0 + 100, y0 + 10], "hl": None, "content": content}


def test_two_column_left_then_right_top_to_bottom():
    """2단: 왼쪽단 위→아래 전부, 그다음 오른쪽단 위→아래(가로지르기 교정)."""
    # PDF y-up: 높은 y0 = 페이지 위쪽
    nodes = [
        _n(1, 50, 700, "A"), _n(1, 380, 700, "D"),   # .md엔 이렇게 가로질러 섞여 들어옴
        _n(1, 50, 600, "B"), _n(1, 380, 600, "E"),
        _n(1, 50, 500, "C"),
    ]
    order = [n["content"] for n in reorder(nodes)]
    assert order == ["A", "B", "C", "D", "E"]


def test_column_boundary_detected_by_gap():
    """x0 갭(>60px)이 단 경계. 왼쪽=0, 오른쪽=1."""
    pn = [_n(1, 50, 700, "L"), _n(1, 380, 700, "R")]
    bounds = page_column_bounds(pn)
    assert len(bounds) == 1                      # 단 경계 1개(2단)
    assert column_index(50, bounds) == 0 and column_index(380, bounds) == 1


def test_single_column_top_to_bottom():
    """단이 하나면(갭 없음) y 내림차순 = 위→아래."""
    nodes = [_n(1, 50, 400, "3"), _n(1, 55, 600, "1"), _n(1, 52, 500, "2")]
    assert [n["content"] for n in reorder(nodes)] == ["1", "2", "3"]


def test_pages_in_ascending_order():
    """페이지는 오름차순으로 이어붙인다."""
    nodes = [_n(2, 50, 700, "p2"), _n(1, 50, 700, "p1")]
    assert [n["content"] for n in reorder(nodes)] == ["p1", "p2"]


def test_flatten_extracts_content_nodes():
    """kids 트리에서 content+bbox 있는 노드만 평탄화(bbox 없는 건 제외)."""
    tree = [{"content": "clause", "bounding box": [10, 20, 30, 40], "page number": 1, "heading level": 2},
            {"content": "no-bbox", "page number": 1},          # bbox 없음 → 제외
            {"kids": [{"content": "nested", "bounding box": [1, 2, 3, 4], "page number": 1}]}]
    got = flatten_nodes(tree)
    assert [n["content"] for n in got] == ["clause", "nested"]
