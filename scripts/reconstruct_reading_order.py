"""다단(multi-column) ODL 문서의 읽기순서 교정 — .json 레이아웃 트리의 page+bounding box로 재정렬.

ODL이 2단 레이아웃 PDF(KB 계열 700~1200p 약관)를 markdown으로 뽑을 때 단을 가로질러(좌·우·좌·우)
읽어 조가 뒤섞인다(실측: KB 골든라이프 보통약관이 .md에선 뒤죽박죽, 재구성 후 제1→53조 정순).
이 스크립트는 각 노드의 page + bounding box로 **page → 단(x0 갭) → y 내림차순**(PDF 좌표는 y가
위로 증가 → 내림차순이 위→아래) 재정렬해 읽기순서를 복원한다.

**주의(실측 한계)**: reading-order 복원은 되나 KB의 clause 수확은 안 늘어난다(parse_compound
987조 ≈ 재구성 982) — KB의 진짜 병목은 "1237p 안 진짜 특약 vs 요약/참조/중복"의 semantic
구분이지 순서가 아님(roadmap 복합파서 절). 즉 이 도구의 값은 (a)다단 문서 **사람 리뷰용
읽기순서 복원**, (b)순서 깨짐이 병목인 다른 다단 문서, (c)KB semantic 세그먼테이션의 전처리.

용법:  uv run python scripts/reconstruct_reading_order.py "<문서명>"   # data/output/raw/<문서>.json → stdout
"""
import os
import sys
import json

HERE = os.path.dirname(__file__)
COL_GAP = 60          # x0 갭이 이보다 크면 단 경계로 본다(px)


def flatten_nodes(tree) -> list[dict]:
    """.json kids 트리 → 텍스트 노드 [{pg, bb:[x0,y0,x1,y1], hl, content}] 평탄화."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            c = o.get("content")
            if isinstance(c, str) and c.strip() and o.get("bounding box"):
                out.append({"pg": o.get("page number"), "bb": o.get("bounding box"),
                            "hl": o.get("heading level"), "content": c})
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(tree)
    return out


def column_index(x0: float, bounds: list[float]) -> int:
    """x0가 몇 번째 단인지 — 단 경계(bounds) 왼쪽부터 0,1,2…"""
    return sum(1 for b in bounds if x0 >= b)


def page_column_bounds(page_nodes: list[dict]) -> list[float]:
    """한 페이지 노드들의 x0 분포에서 단 경계 검출(COL_GAP 이상 갭 = 경계)."""
    xs = sorted(n["bb"][0] for n in page_nodes)
    bounds = []
    for i in range(1, len(xs)):
        if xs[i] - xs[i - 1] > COL_GAP:
            bounds.append((xs[i - 1] + xs[i]) / 2)
    return bounds


def reorder(nodes: list[dict]) -> list[dict]:
    """읽기순서 재정렬: page 오름차순 → 단(왼쪽 먼저) → y 내림차순(위→아래, PDF y-up)."""
    from collections import defaultdict
    by_page = defaultdict(list)
    for n in nodes:
        by_page[n["pg"]].append(n)
    ordered = []
    for pg in sorted(k for k in by_page if k is not None):
        pn = by_page[pg]
        bounds = page_column_bounds(pn)
        pn.sort(key=lambda n: (column_index(n["bb"][0], bounds), -n["bb"][1]))
        ordered += pn
    return ordered


def to_markdown(ordered: list[dict]) -> str:
    """재정렬 노드 → markdown(heading level → #)."""
    lines = []
    for n in ordered:
        h = n.get("hl")
        prefix = "#" * min(h, 6) + " " if isinstance(h, int) and h > 0 else ""
        lines.append(prefix + n["content"])
    return "\n".join(lines)


def reconstruct(json_path: str) -> str:
    tree = json.load(open(json_path, encoding="utf-8")).get("kids", [])
    return to_markdown(reorder(flatten_nodes(tree)))


def main():
    if len(sys.argv) < 2:
        print("용법: reconstruct_reading_order.py <문서명>", file=sys.stderr)
        sys.exit(2)
    doc = sys.argv[1]
    path = os.path.join(HERE, "..", "data", "output", "raw", f"{doc}.json")
    if not os.path.exists(path):
        # 부분매칭 폴백
        import glob
        cand = [p for p in glob.glob(os.path.join(HERE, "..", "data", "output", "raw", "*.json"))
                if doc in os.path.basename(p)]
        if not cand:
            print(f"문서 없음: {doc}", file=sys.stderr)
            sys.exit(1)
        path = cand[0]
    sys.stdout.write(reconstruct(path))


if __name__ == "__main__":
    main()
