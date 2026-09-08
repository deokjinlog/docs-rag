"""공통 블록 스키마 — ODL·OCR·VL 출력을 한 모양으로 묶는다. (캐스케이드 M2)

**왜 필요한가** — `reconstruct_reading_order` 의 bbox 재정렬이 **읽기순서를 실제로 고치는
유일한 도구**다(실측: ODL java·ODL 하이브리드·PaddleOCR-VL 셋 다 다단에서 틀렸고 이것만
맞았다, docs/parser-vl-eval.md). 그런데 지금은 ODL JSON 트리에만 먹는다.
세 경로 출력을 같은 모양으로 만들면 **OCR·VL 결과에도 그대로 적용**되고, 그 뒤 단계
(청킹·조 파싱)는 어느 경로로 왔는지 몰라도 된다.

재정렬 로직(`page_column_bounds`·`column_index`·`reorder`)은 원래부터 `pg`·`bb` 만 쓰므로
소스 무관이었다. 소스에 묶여 있던 건 **입구(flatten)** 뿐이라, 어댑터만 갈아끼우면 된다.

**전폭 요소 버그도 여기서 고친다.** 기존 구현은 단을 x0 로만 갈라서, 두 단을 가로지르는
제목이 "왼쪽 단"으로 분류돼 본문 사이에 끼었다(실측: 제목 bbox x0=176, x1=424 가
단 경계 ~300 을 가로지르는데 x0<300 이라 0번 단으로 갔다). 전폭 요소는 어느 단에도
속하지 않으므로 **밴드 경계**로 쓴다 — 그게 페이지 제목·중간 표를 제자리에 놓는다.
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Literal

COL_GAP = 60          # x0 갭이 이보다 크면 단 경계(px). reconstruct_reading_order 와 같은 값

BlockType = Literal["heading", "text", "table", "figure", "caption"]
Source = Literal["odl", "ocr", "vl"]


@dataclass
class Block:
    """파싱 결과의 최소 단위. 세 경로가 이걸로 수렴한다."""
    page: int
    bbox: tuple[float, float, float, float]     # (x0, y0, x1, y1) — PDF 좌표(y 위로 증가)
    text: str
    type: BlockType = "text"
    source: Source = "odl"
    confidence: float | None = None             # OCR 만 채운다. 격상 판단에 쓴다(M5)
    heading_level: int | None = None            # 1~6. markdown '#' 개수
    font_size: float | None = None              # ODL 이 준다. heading 판정 근거로 쓸 수 있다
    order: int | None = None                    # reorder() 가 채운다
    meta: dict = field(default_factory=dict)

    @property
    def x0(self) -> float: return self.bbox[0]
    @property
    def y0(self) -> float: return self.bbox[1]
    @property
    def x1(self) -> float: return self.bbox[2]
    @property
    def y1(self) -> float: return self.bbox[3]


# ── 어댑터 ────────────────────────────────────────────────────────────────────

def from_odl_json(path: str | pathlib.Path) -> list[Block]:
    """ODL 레이아웃 트리(.json) → Block.

    ODL 은 `kids` 재귀 트리에 bounding box·font·font size·type 을 담아준다.
    마크다운으로 변환되면서 폰트 정보가 사라지므로, heading 판정을 하려면 여기서 받아야 한다.
    """
    tree = json.loads(pathlib.Path(path).read_text(encoding="utf-8")).get("kids", [])
    out: list[Block] = []

    def walk(o):
        if isinstance(o, dict):
            c, bb = o.get("content"), o.get("bounding box")
            if isinstance(c, str) and c.strip() and bb:
                b = json.loads(bb) if isinstance(bb, str) else bb
                pg = o.get("page number")
                fs = o.get("font size")
                out.append(Block(
                    page=int(pg) if pg is not None else 0,
                    bbox=tuple(float(v) for v in b[:4]),
                    text=c,
                    type=_odl_type(o.get("type")),
                    source="odl",
                    heading_level=o.get("heading level"),
                    font_size=float(fs) if fs not in (None, "") else None,
                    meta={"pdfua_tag": o.get("pdfua_tag")},
                ))
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(tree)
    return out


def _odl_type(t: str | None) -> BlockType:
    t = (t or "").lower()
    if "table" in t:
        return "table"
    if "head" in t or "title" in t:
        return "heading"
    if "image" in t or "figure" in t:
        return "figure"
    if "caption" in t:
        return "caption"
    return "text"


def from_ocr(res: dict, page: int = 0) -> list[Block]:
    """PP-StructureV3 결과 → Block.

    `overall_ocr_res` 의 `rec_texts` / `rec_scores` / `dt_polys` 를 줄 단위로 편다.
    **confidence 를 반드시 실어야 한다** — M5 의 줄 단위 격상이 이 값으로 돈다
    (실측: 잘린 글자 '움' 이 0.117 로 잡혔고 정상 줄은 0.95+ 였다).
    """
    o = (res or {}).get("overall_ocr_res") or {}
    texts = o.get("rec_texts") or []
    scores = o.get("rec_scores") or []
    polys = o.get("dt_polys") or o.get("rec_polys") or []
    out = []
    for i, t in enumerate(texts):
        if not (t or "").strip():
            continue
        bb = _poly_to_bbox(polys[i]) if i < len(polys) else (0.0, 0.0, 0.0, 0.0)
        out.append(Block(page=page, bbox=bb, text=t, source="ocr",
                         confidence=float(scores[i]) if i < len(scores) else None))
    return out


def _poly_to_bbox(poly) -> tuple[float, float, float, float]:
    """4점 폴리곤 → (x0,y0,x1,y1). OCR 은 회전 사각형을 주므로 축정렬 박스로 편다."""
    try:
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def from_vl_markdown(md: str, page: int = 0, layout: list | None = None) -> list[Block]:
    """PaddleOCR-VL 마크다운 → Block.

    VL 은 마크다운을 내주므로 bbox 가 블록별로 붙지 않는다. `layout`(레이아웃 검출 결과)이
    있으면 순서대로 매칭하고, 없으면 **bbox 없이** 문단 순서만 보존한다.
    bbox 가 없으면 재정렬을 못 하므로 VL 은 되도록 layout 과 함께 받는 게 좋다 —
    이게 M4 에서 VL 어댑터에 손이 가는 이유다.
    """
    out = []
    for i, para in enumerate([p for p in re.split(r"\n{2,}", md) if p.strip()]):
        m = _MD_HEADING.match(para.strip().splitlines()[0])
        lvl = len(m.group(1)) if m else None
        bb = (0.0, 0.0, 0.0, 0.0)
        typ: BlockType = "heading" if lvl else ("table" if para.lstrip().startswith("|") else "text")
        if layout and i < len(layout):
            b = layout[i]
            bb = tuple(float(v) for v in (b.get("bbox") or b.get("coordinate") or bb)[:4])
        out.append(Block(page=page, bbox=bb, text=para, type=typ, source="vl",
                         heading_level=lvl, order=i))
    return out


# ── 읽기순서 재구성 ───────────────────────────────────────────────────────────

def page_column_bounds(blocks: list[Block]) -> list[float]:
    """한 페이지 블록들의 x0 분포에서 단 경계 검출(COL_GAP 이상 갭 = 경계).

    기존 `reconstruct_reading_order.page_column_bounds` 와 **완전히 같다.**
    (군집 크기 필터를 넣어봤으나 실제 문서에서 오름차순 비율이 떨어져 되돌렸다 —
     M2 의 계약은 "스키마만 바꾸고 결과는 그대로"다.)
    """
    xs = sorted(b.x0 for b in blocks)
    return [(xs[i - 1] + xs[i]) / 2 for i in range(1, len(xs)) if xs[i] - xs[i - 1] > COL_GAP]


def column_index(b: Block, bounds: list[float]) -> int:
    """블록이 몇 번째 단인지 — x0 기준. 기존 구현과 같다.

    ⚠ **전폭 요소를 구분하지 못한다.** 두 단을 가로지르는 제목이 x0 가 속한 단으로
    분류돼 본문 사이에 낀다(실측: 제목 x0=176·x1=424 가 경계 ~300 을 가로지르는데
    x0<300 이라 0번 단). `spans_columns()` 로 판별은 되지만 **정렬에는 안 쓴다** —
    reorder 주석의 실측 회귀 때문이다.
    """
    return sum(1 for bd in bounds if b.x0 >= bd)


def spans_columns(b: Block, bounds: list[float]) -> bool:
    """블록이 단 경계를 가로지르나(= 전폭). 판별만 하고 정렬에는 아직 안 쓴다."""
    return any(b.x0 < bd < b.x1 for bd in bounds)


def reorder(blocks: list[Block]) -> list[Block]:
    """읽기순서 재정렬 — page → 단(왼쪽 먼저) → y 내림차순(PDF y-up).

    기존 `reconstruct_reading_order.reorder` 와 **결과가 동일해야 한다**(실제 문서 4종
    회귀 0 으로 검증). M2 의 목적은 알고리즘 개선이 아니라 **입력 스키마 통일**이다 —
    이게 되면 같은 재정렬을 OCR·VL 결과에도 그대로 먹일 수 있다.

    ⚠ **밴드 분할은 시도했다가 되돌렸다.** 전폭 요소를 밴드 경계로 써서
    "좌단 → 우단 → 전폭표 → 좌단 → 우단" 을 맞추려 했고 합성 테스트는 통과했으나,
    실제 KB 약관 4종에서 오름차순 비율이 **전부 떨어졌다**
    (0.669→0.625 · 0.694→0.639 · 0.726→0.657 · 0.688→0.632).
    원인은 전폭 요소가 너무 많다는 것 — 실측: 다단 페이지 394개에서 페이지당 블록 p50=34,
    **전폭 p50=16**. 밴드가 17개로 쪼개지면 밴드당 2블록이라 단 정렬이 무력해진다
    (헤더·푸터·페이지번호·구분선이 전부 전폭으로 잡힌다).
    → 밴드를 쓰려면 "진짜 전폭"을 더 좁게 정의해야 한다(페이지 폭 대비 비율·폰트 크기·
      헤더푸터 영역 제외). 신호를 더 봐야 하는 별건이라 지금은 안 쓴다.
    """
    by_page: dict[int, list[Block]] = collections.defaultdict(list)
    for b in blocks:
        by_page[b.page].append(b)

    ordered: list[Block] = []
    for pg in sorted(by_page):
        pn = by_page[pg]
        bounds = page_column_bounds(pn)
        # ⚠ **y0(아래 모서리)** 로 정렬한다. 기존 구현이 `bb[1]` 을 쓰기 때문이고,
        # M2 의 계약이 "결과 동일"이라 맞춘다. 읽기순서로는 위 모서리(y1)가 더 자연스러워
        # 보이지만 실측은 y0 가 근소 우세였다(오름차순 0.669 vs 0.668) — 바꿀 이유가 없다.
        pn.sort(key=lambda b: (column_index(b, bounds), -b.y0))
        ordered += pn

    for i, b in enumerate(ordered):
        b.order = i
    return ordered


def to_markdown(blocks: list[Block]) -> str:
    """Block → markdown. heading_level 이 있으면 '#' 로 승격."""
    lines = []
    for b in blocks:
        h = b.heading_level
        prefix = "#" * min(h, 6) + " " if isinstance(h, int) and h > 0 else ""
        lines.append(prefix + b.text)
    return "\n".join(lines)


def reconstruct(json_path: str | pathlib.Path) -> str:
    """ODL JSON → 읽기순서 복원 markdown. 기존 reconstruct_reading_order 와 같은 계약."""
    return to_markdown(reorder(from_odl_json(json_path)))
