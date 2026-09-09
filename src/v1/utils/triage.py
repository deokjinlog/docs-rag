"""페이지 triage — 라우팅 판정 + 난이도 태그. 규칙 기반, 모델 없음.

**왜 규칙인가** — 98% 정확도 분류기도 1,000건이면 20건을 엉뚱한 경로로 보낸다. 규칙은
틀리는 방식이 예측 가능해 디버깅되고 페이지당 수 ms 다. precision-first 와 같은 규율.

**입력이 둘인 이유** — 신규 문서는 PDF 가 있으니 PyMuPDF 로 정확히 재고, 이미 색인된 문서는
PDF 를 다시 열지 않고 **ODL 트리에서 파생**한다(백필용). 파생은 6개 신호 중 5개만 되고
`rotation` 은 트리에 없다. 그리고 파생값은 *"PDF 에 뭐가 있나"* 가 아니라 *"ODL 이 뭘
내놨나"* 를 잰다 — "텍스트 레이어가 없다"와 "ODL 이 실패했다"를 구분 못 한다. 다만 **둘 다
SCAN 판정으로 가야 하는 건 같아서** 라우팅 목적에는 지장이 없다. 출처는 `source` 로 남긴다.

임계치는 전부 `configs/triage.yaml` 에 있고 값마다 실측 근거가 붙어 있다.
"""
from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

# ⚠ **v1.config 를 import 하지 않는다.** 이 모듈은 순수 규칙이라 스크립트·테스트에서
# 스택 없이 돌아야 하는데, v1.config 는 __init__ 에서 database 를 끌어와 sqlalchemy 까지
# 딸려온다(프로젝트 규약상 __init__ 은 light 만 re-export 해야 하지만 현재는 그렇지 않다).
# 경로 하나 때문에 무거운 의존을 지는 건 손해라 여기서 직접 구한다.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

CONFIG_PATH = PROJECT_ROOT / "configs" / "triage.yaml"

# 분리 자모(조합 안 된 한글)·치환문자 — 추출 실패의 전형적 흔적.
# 한글 PDF 의 흔한 실패는 "텍스트가 없다"가 아니라 **"있는데 깨졌다"** 다.
_JAMO = re.compile(r"[ᄀ-ᇿ㄰-㆏]")
# ⚠ **\x01~\x08 은 뺐다.** 실측(finished 12문서 86p 표본): 이 구간 문자가 **38% 페이지**에
# 있고 비율 중앙값이 **0.157** 로 임계 0.15 를 넘겨, 코퍼스의 1/4 이 NATIVE_SUSPECT 로
# 오분류됐다. 실제로는 손상이 아니라 **단어 구분자** 대용이다
# ("항문질환\x01치료\x01및\x01한의원\x01치료는" — 주변 한글이 멀쩡하다).
# 진짜 손상 마커인 U+FFFD 는 같은 표본에서 **0건**이었다. 구분자는 정규화 단계에서
# 공백으로 치환할 대상이지 신뢰도 신호가 아니다 → separator_ratio 로 따로 센다.
_CTRL = re.compile(r"[\x00\x0b\x0c\x0e-\x1f\ufffd]")
_SEP_CTRL = re.compile(r"[\x01-\x08]")

# 텍스트로 취급하는 블록 타입 (ODL 트리 기준). 표·그림 컨테이너는 제외.
TEXTY = {"paragraph", "list item", "heading", "caption", "text block"}

# 도메인 앵커 — 약관·고시라면 반드시 나오는 구조 표지. 이게 0건인데 글자 수는 정상이면
# "글자는 읽었는데 문서 구조가 안 보인다"는 뜻이고, conf 가 높아도 오인식일 수 있다
# (예: 제l2조 — 숫자 1 이 소문자 l 로). confidence 로는 못 잡는 실패라 따로 센다.
_ANCHOR = re.compile(r"제\s*\d+\s*[조항호장절관]|[①-⑳]|^\s*[가-힣]\.\s|^\s*\d+\.\s", re.M)


def anchor_hits(text: str) -> int:
    return len(_ANCHOR.findall(text or ""))


def font_flags_of(page) -> list[str]:
    """ToUnicode 없음 · Type3 — 추출문이 조용히 틀어지는 원인."""
    flags: set[str] = set()
    try:
        doc = page.parent
        for f in page.get_fonts(full=True):
            xref, _ext, ftype = f[0], f[1], f[2]
            if str(ftype).lower().startswith("type3"):
                flags.add("type3")
            try:
                if not doc.xref_get_key(xref, "ToUnicode")[0] or \
                        doc.xref_get_key(xref, "ToUnicode")[0] == "null":
                    flags.add("no_tounicode")
            except Exception:
                pass
    except Exception:
        pass
    return sorted(flags)


@functools.lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict:
    import yaml
    p = Path(path) if path else CONFIG_PATH
    return yaml.safe_load(p.read_text(encoding="utf-8"))


@dataclass
class PageSignals:
    """한 페이지의 원신호. 판정·태그는 전부 여기서 파생된다."""
    page: int
    page_w: float
    page_h: float
    char_count: int = 0
    image_cover: float = 0.0
    garbage_ratio: float = 0.0
    table_area_ratio: float = 0.0
    table_count: int = 0
    cell_count: int = 0
    gutter_pt: float = 0.0
    gutter_x: float | None = None
    left_blocks: int = 0
    right_blocks: int = 0
    median_font_pt: float | None = None
    rotation: int | None = None          # ODL 파생에서는 None (트리에 없다)
    source: str = "pymupdf"              # pymupdf | odl_tree
    # 폰트 결함 — 글자는 뽑히는데 **의미가 틀린** 경우를 잡는다. ToUnicode 없는 서브셋 폰트나
    # Type3 는 코드포인트 매핑이 없어 추출문이 그럴듯한 쓰레기가 된다. garbage_ratio 는
    # 자모 분리·U+FFFD 만 보므로 이런 '조용한' 깨짐을 놓친다 → 별도 신호로 둔다.
    separator_ratio: float = 0.0         # \x01~\x08 — 정규화 대상이지 손상 아님
    font_suspect: bool = False
    font_flags: list[str] = field(default_factory=list)
    anchor_hits: int = 0                 # 제N조·①·가. 매칭 수 (VL 격상 판단용)
    # 이미지 기반 신호 — 스캔 페이지에서만 채운다(비싸다)
    hist_span: int | None = None
    sharpness: float | None = None
    illum_lowfreq: float | None = None
    skew_deg: float | None = None
    # OCR/VL 산출물에서만 오는 것
    labels: list[str] = field(default_factory=list)
    figure_text_chars: int = 0
    ocr_conf_lt80_ratio: float | None = None


# ── 공통 계산 ────────────────────────────────────────────────────────────────
def garbage_ratio(text: str) -> float:
    """못 믿을 문자 비율. 구분자 대용 제어문자(\x01~\x08)는 **세지 않는다**(위 주석)."""
    if not text:
        return 0.0
    bad = len(_CTRL.findall(text)) + len(_JAMO.findall(text))
    return bad / len(text)


def separator_ratio(text: str) -> float:
    """\x01~\x08 비율. 손상이 아니라 **정규화 대상**(공백 치환)임을 표시하는 신호."""
    if not text:
        return 0.0
    return len(_SEP_CTRL.findall(text)) / len(text)


def _gutter(spans: list[tuple[float, float]], page_w: float, band: tuple[float, float],
            ) -> tuple[float, float | None]:
    """페이지 중앙부에서 **어떤 텍스트 블록도 덮지 않는 최대 x-구간**(세로 홈통)의 폭·중심.

    다단 판정의 핵심. x0 클러스터링으로 세면 중첩 목록의 들여쓰기가 단으로 잡혀
    41.5% 가 다단이 된다(실측) — 홈통은 그 오검출을 안 낸다.
    """
    if len(spans) < 6 or page_w <= 0:
        return 0.0, None
    w = int(page_w)
    covered = bytearray(w)
    for a, b in spans:
        lo = max(0, int(a))
        hi = min(w, int(b) + 1)
        if hi > lo:
            covered[lo:hi] = b"\x01" * (hi - lo)
    lo_i, hi_i = int(band[0] * w), int(band[1] * w)
    best = run = 0
    best_pos = None
    for x in range(lo_i, hi_i):
        if covered[x]:
            run = 0
        else:
            run += 1
            if run > best:
                best, best_pos = run, x - run // 2
    return float(best), (float(best_pos) if best_pos is not None else None)


# ── 입력 어댑터 ①: PyMuPDF (신규 문서·정확) ───────────────────────────────────
def signals_from_page(page, cfg: dict | None = None) -> PageSignals:
    cfg = cfg or load_config()
    band = tuple(cfg["tags"]["multi_column"]["search_band"])
    # ⚠ A4 세로를 가정하면 안 된다 — 관악구 명세서는 841x595(가로)다. 실제 크기를 읽는다.
    pw, ph = float(page.rect.width), float(page.rect.height)
    text = page.get_text("text")
    area = abs(pw * ph) or 1.0

    img_area = 0.0
    for info in page.get_images(full=True):
        try:
            for r in page.get_image_rects(info[0]):
                img_area += abs(r.width * r.height)
        except Exception:
            pass

    tarea = 0.0
    tcount = 0
    cells = 0
    try:
        tables = page.find_tables().tables
        tcount = len(tables)
        for t in tables:
            x0, y0, x1, y1 = t.bbox
            tarea += abs((x1 - x0) * (y1 - y0))
            try:
                cells += len(t.cells)
            except Exception:
                pass
    except Exception:
        pass

    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    g, gx = _gutter([(b[0], b[2]) for b in blocks], pw, band)
    left = sum(1 for b in blocks if gx is not None and b[2] <= gx)
    right = sum(1 for b in blocks if gx is not None and b[0] >= gx)

    sizes = [s["size"] for b in page.get_text("dict")["blocks"] if b.get("type") == 0
             for l in b["lines"] for s in l["spans"] if s.get("text", "").strip()]
    return PageSignals(
        page=page.number + 1, page_w=pw, page_h=ph,
        char_count=len(text.strip()),
        image_cover=round(min(img_area / area, 1.0), 3),
        garbage_ratio=round(garbage_ratio(text), 4),
        table_area_ratio=round(tarea / area, 4), table_count=tcount, cell_count=cells,
        gutter_pt=g, gutter_x=gx, left_blocks=left, right_blocks=right,
        median_font_pt=(sorted(sizes)[len(sizes) // 2] if sizes else None),
        rotation=page.rotation, source="pymupdf",
        separator_ratio=round(separator_ratio(text), 4),
        font_flags=(ff := font_flags_of(page)),
        # ⚠ 폰트 플래그 **단독으로는 의심하지 않는다.** 실측: finished 12문서에서 0건이고,
        # 유일하게 걸린 문서(관악구 명세서 25p 전량)는 추출문이 육안으로 정확했다.
        # ToUnicode 가 없어도 인코딩으로 복원되는 경우가 흔하다 → 실제 깨짐 흔적이
        # 함께 있을 때만 의심한다. 안 그러면 멀쩡한 문서를 통째로 비싼 경로로 보낸다.
        font_suspect=bool(ff) and garbage_ratio(text) > 0.02,
        anchor_hits=anchor_hits(text),
    )


# ── 입력 어댑터 ②: ODL 트리 (기존 색인 문서·백필) ─────────────────────────────
def flatten_odl(tree) -> list[dict]:
    """bbox 를 가진 **모든** 노드를 평탄화. 텍스트 없는 image·table·list 컨테이너도 살린다.

    기존 `reconstruct_reading_order.flatten_nodes` 는 content 가 있는 노드만 남기는데,
    그러면 그림·표 영역이 통째로 사라져 태그(table_dense·figure)를 못 만든다.
    트리는 최상위만 평탄하고 `list items → kids` 로 중첩된다(실측 1,972노드/88p).
    """
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            bb = o.get("bounding box")
            if bb and len(bb) == 4:
                out.append({"pg": o.get("page number"), "t": o.get("type"),
                            "bb": [float(v) for v in bb],
                            "c": (o.get("content") or ""),
                            "fs": o.get("font size")})
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(tree)
    return out


def page_size_from_nodes(nodes: list[dict]) -> tuple[float, float]:
    """트리에는 페이지 크기가 없다 → bbox 최대 extent 로 추정하고 표준 용지로 스냅.
    가로 문서를 세로로 오인하면 면적 비율·상하단 밴드가 전부 틀어진다."""
    if not nodes:
        return 595.0, 842.0
    w = max(n["bb"][2] for n in nodes)
    h = max(n["bb"][3] for n in nodes)
    for pw, ph in ((595.0, 842.0), (842.0, 595.0), (612.0, 792.0), (792.0, 612.0)):
        if w <= pw + 12 and h <= ph + 12:
            return pw, ph
    return math.ceil(w), math.ceil(h)


def signals_from_odl(nodes: list[dict], page_no: int, cfg: dict | None = None) -> PageSignals:
    """한 페이지분 노드에서 파생. `rotation` 은 만들 수 없다(트리에 없음)."""
    cfg = cfg or load_config()
    band = tuple(cfg["tags"]["multi_column"]["search_band"])
    pw, ph = page_size_from_nodes(nodes)
    area = pw * ph or 1.0
    texty = [n for n in nodes if n["t"] in TEXTY and n["c"].strip()]
    text = "\n".join(n["c"] for n in texty)

    tarea = sum(abs((n["bb"][2] - n["bb"][0]) * (n["bb"][3] - n["bb"][1]))
                for n in nodes if n["t"] == "table")
    img_area = sum(abs((n["bb"][2] - n["bb"][0]) * (n["bb"][3] - n["bb"][1]))
                   for n in nodes if n["t"] == "image")
    g, gx = _gutter([(n["bb"][0], n["bb"][2]) for n in texty], pw, band)
    sizes = [n["fs"] for n in texty if isinstance(n.get("fs"), (int, float)) and n["fs"] > 0]
    return PageSignals(
        page=page_no, page_w=pw, page_h=ph,
        char_count=len(text.strip()),
        image_cover=round(min(img_area / area, 1.0), 3),
        garbage_ratio=round(garbage_ratio(text), 4),
        table_area_ratio=round(tarea / area, 4),
        table_count=sum(1 for n in nodes if n["t"] == "table"),
        cell_count=sum(1 for n in nodes if n["t"] == "table cell"),
        gutter_pt=g, gutter_x=gx,
        left_blocks=sum(1 for n in texty if gx is not None and n["bb"][2] <= gx),
        right_blocks=sum(1 for n in texty if gx is not None and n["bb"][0] >= gx),
        median_font_pt=(sorted(sizes)[len(sizes) // 2] if sizes else None),
        rotation=None, source="odl_tree",
        # 폰트 결함은 트리에서 알 수 없다 — ODL 이 이미 추출을 끝낸 뒤라 원본 폰트 딕셔너리가
        # 없다. 백필 판정이 신규 판정보다 관대해질 수 있다는 뜻이라 source 로 구분해 둔다.
        separator_ratio=round(separator_ratio(text), 4),
        anchor_hits=anchor_hits(text),
    )


# ── 판정 ─────────────────────────────────────────────────────────────────────
# **3값이다.** 예전엔 SCAN 을 SIMPLE/COMPLEX 로 갈라 각각 OCR/VL 로 보냈는데,
# PP-StructureV3 가 이미 레이아웃 탐지를 포함하므로(실측: layout_det_res 로 표·그림 라벨을
# 그대로 준다) 스캔 페이지를 미리 둘로 가를 이유가 없다. VL 은 페이지 라우팅이 아니라
# **OCR 결과를 보고 하는 격상**이고, 격상 단위도 페이지가 아니라 줄/영역이다.
ROUTE_TARGET = {"NATIVE": "odl", "NATIVE_SUSPECT": "odl_vs_ocr", "SCAN": "ocr"}


def classify(s: PageSignals, cfg: dict | None = None) -> str:
    """3-값 판정. 순서가 곧 우선순위 — 싼 경로가 기본, 신호가 있을 때만 격상.

    NATIVE         텍스트 레이어를 믿는다            → ODL + bbox 재구성
    NATIVE_SUSPECT 레이어는 있는데 못 믿는다          → ODL·OCR 둘 다 뽑아 유효 음절 비율로 선택
    SCAN           레이어가 없다                     → PP-StructureV3(OCR)
    """
    r = (cfg or load_config())["route"]
    # ⚠ 순서가 곧 로직이다. char_count 로 먼저 통과시키면 **깨진 텍스트가 NATIVE 로 새어**
    # 조용히 쓰레기를 색인한다. 한글 PDF 의 흔한 실패는 '없다'가 아니라 '있는데 깨졌다'.
    if s.char_count >= r["char_min"]:
        if s.garbage_ratio > r["garbage_max"] or s.font_suspect:
            return "NATIVE_SUSPECT"
        return "NATIVE"
    return "SCAN"


def difficulty_tags(s: PageSignals, cfg: dict | None = None, engine: str = "odl") -> list[str]:
    """중복 가능한 태그 목록. 엔진 조건부 규칙은 `engine` 으로 갈린다."""
    c = (cfg or load_config())["tags"]
    tags: list[str] = []

    mc = c["multi_column"]
    if (s.gutter_pt >= mc["gutter_min_pt"]
            and s.left_blocks >= mc["side_blocks_min"]
            and s.right_blocks >= mc["side_blocks_min"]):
        tags.append("multi_column")

    td = c["table_dense"]
    dense = s.table_area_ratio >= td["area_ratio_min"]
    if not dense and engine in td["cell_rule_engines"]:
        dense = s.cell_count >= td["cell_count_min"]
    if dense:
        tags.append("table_dense")

    tt = c["tiny_text"]
    if tt.get("native_pt_max") and s.median_font_pt is not None \
            and s.median_font_pt < tt["native_pt_max"]:
        tags.append("tiny_text")

    os_ = c["old_scan"]
    if s.image_cover >= os_["image_cover_min"] and (
            (s.hist_span is not None and s.hist_span < os_["hist_span_max"])
            or (s.sharpness is not None and s.sharpness < os_["sharpness_max"])):
        tags.append("old_scan")

    ph = c["photographed"]
    if (s.skew_deg is not None and s.skew_deg >= ph["skew_deg_min"]) or \
       (s.illum_lowfreq is not None and s.illum_lowfreq > ph["illum_lowfreq_dev_max"]):
        tags.append("photographed")

    if c["formula"]["label"] in s.labels:
        tags.append("formula")

    fl = c["figure_labeled"]
    if any(l in fl["labels"] for l in s.labels) and s.figure_text_chars >= fl["ocr_chars_min"]:
        tags.append("figure_labeled")

    return tags
