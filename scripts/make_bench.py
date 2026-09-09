"""벤치셋 생성 (B1) — **정답이 딸린** 파싱 평가 세트를 난이도 태그와 1:1로 만든다.

**정답이 어디서 오나**: 우리 코퍼스는 네이티브 PDF 라 **텍스트 레이어가 곧 정답**이다.
그걸 이미지로 구운 뒤 다시 읽게 하면 OCR·VL 을 채점할 수 있다. 표 구조 정답은 pdfplumber
`find_tables()` 의 셀 격자에서 만든다(ODL 셀은 과소 추출이라 정답으로 못 쓴다 — 표 보유
페이지 셀 중앙값이 6개, 같은 페이지를 PyMuPDF 로 재면 1,380개).

**두 세트를 분리한다**:
  synthetic — 네이티브 페이지를 래스터라이즈 + 열화. 정답 있음(Text·Table·ReadingOrder·Layout)
  real      — 텍스트 레이어가 없는 **진짜 스캔** 페이지. 정답 없음 → 교차 확인용으로만

**변형은 난이도 태그와 1:1로 대응한다** — 그래야 행렬의 '태그' 축이 의미를 갖는다:
    clean300 기준선 / fax150·dark → old_scan / skew2·skew5·shadow → photographed / shrink → tiny_text

용법:
    docker compose exec celery python /app/src/../scripts/make_bench.py --help   # (권장 아님)
    python3 scripts/make_bench.py --out data/bench --per-tag 12
  celery 컨테이너 안에 pymupdf·pdfplumber·PIL 이 다 있으므로 거기서 도는 게 가장 쉽다.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

# 변형 정의 — make_scanset 의 것을 재사용한다(중복 구현이 갈라지는 걸 막는다).
from make_scanset import VARIANTS, _degrade  # noqa: E402

BENCH_VARIANTS = {
    "clean300": dict(dpi=300),
    "fax150":   VARIANTS["fax150"],
    "skew2":    dict(dpi=200, angle=2.0),
    "skew5":    dict(dpi=200, angle=5.0),
    "shadow":   VARIANTS["shadow"],
    "dark":     VARIANTS["dark"],
    "shrink":   VARIANTS["shrink"],
}
VARIANT_TAG = {
    "clean300": None, "fax150": "old_scan", "dark": "old_scan",
    "skew2": "photographed", "skew5": "photographed", "shadow": "photographed",
    "shrink": "tiny_text",
}

MIN_TRUTH_CHARS = 300
CELL_AGREE = 0.20      # ODL 대비 ±20% 안이면 TEDS 채점 대상


def _tables_html(page) -> tuple[list[str], int]:
    """pdfplumber 셀 격자 → 단순 HTML. 반환 `(html 목록, 총 셀 수)`.

    TEDS 는 **구조** 유사도라 셀 병합·행열 수가 정답의 핵심이다. 텍스트는 참고로만 넣는다.
    """
    import pdfplumber  # noqa: F401  (호출부가 page 를 pdfplumber 로 연다)
    out, cells = [], 0
    for t in page.find_tables():
        rows = t.extract()
        if not rows:
            continue
        cells += sum(len(r) for r in rows)
        body = "".join(
            "<tr>" + "".join(f"<td>{(c or '').strip()}</td>" for c in r) + "</tr>"
            for r in rows
        )
        out.append(f"<table>{body}</table>")
    return out, cells


def _truth_blocks(page, cfg: dict) -> list[dict]:
    """레이아웃 정답 — 텍스트 블록 bbox와 읽기순서.

    읽기순서 정답을 bbox 로 정하는 근거: 실측에서 ODL·hybrid·VL **셋 다** 다단 순서를
    틀렸고 bbox 재구성만 맞췄다. 즉 엔진 출력은 정답이 될 수 없다.

    ⚠ **좌표계 두 개를 섞으면 안 된다.** PyMuPDF `get_text("blocks")` 는 좌상단 원점
    (y-down)이라 위→아래가 **y 오름차순**이다. 반면 ODL JSON 은 PDF point(y-up)라
    `reconstruct_reading_order.reorder` 는 y 내림차순으로 정렬한다. 그 코드를 그대로
    가져다 쓰면 페이지가 위아래로 뒤집힌다.

    ⚠ **단 판정은 x0 클러스터링으로 하지 않는다.** 한국어 약관의 중첩 목록 들여쓰기가
    단 경계로 잡혀 단일 단 페이지의 순서를 흩뜨린다(캐스케이드 M2 에서 실제로 회귀했다).
    검증된 '중앙부 홈통' 규칙으로 2단인지 먼저 판정하고, 아니면 순수하게 y 로만 정렬한다.
    """
    from v1.utils.triage import _gutter

    raw = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    blocks = [{"bbox": [round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1)],
               "text": b[4].strip(), "type": "text"} for b in raw]
    if not blocks:
        return []

    mc = cfg["tags"]["multi_column"]
    g, gx = _gutter([(b[0], b[2]) for b in raw], float(page.rect.width),
                    tuple(mc["search_band"]))
    left = sum(1 for b in raw if gx is not None and b[2] <= gx)
    right = sum(1 for b in raw if gx is not None and b[0] >= gx)
    two_col = (g >= mc["gutter_min_pt"] and left >= mc["side_blocks_min"]
               and right >= mc["side_blocks_min"])

    def key(i):
        b = blocks[i]["bbox"]
        col = 1 if (two_col and gx is not None and b[0] >= gx) else 0
        return (col, b[1], b[0])        # y-down → 오름차순이 위→아래

    for rank, idx in enumerate(sorted(range(len(blocks)), key=key)):
        blocks[idx]["read_order"] = rank
    return blocks


def collect(sources: list[pathlib.Path], per_tag: int, seed: int) -> tuple[list, list]:
    """(합성 후보, 실제 스캔 후보). 태그별로 상한을 두고 문서 다양성을 우선한다."""
    import pymupdf
    from v1.utils.triage import load_config, signals_from_page, classify, difficulty_tags

    cfg = load_config()
    by_tag: dict[str, list] = collections.defaultdict(list)
    scans: list = []
    for src in sources:
        try:
            doc = pymupdf.open(src)
        except Exception as e:
            print(f"  ⚠ 열기 실패 {src.name[:40]}: {type(e).__name__}", file=sys.stderr)
            continue
        for i in range(len(doc)):
            page = doc[i]
            s = signals_from_page(page, cfg)
            v = classify(s, cfg)
            if v != "NATIVE":
                scans.append({"doc": src.name, "page": i + 1, "verdict": v,
                              "tags": difficulty_tags(s, cfg, "odl")})
                continue
            if s.char_count < MIN_TRUTH_CHARS:
                continue                      # 정답이 짧으면 채점이 요동친다
            tags = difficulty_tags(s, cfg, "odl") or ["plain"]
            for t in tags:
                by_tag[t].append({"doc": src.name, "path": str(src), "page": i + 1,
                                  "tags": tags, "chars": s.char_count})
        doc.close()

    rnd = random.Random(seed)
    picked, seen = [], set()
    for tag, cands in sorted(by_tag.items()):
        # 문서 다양성 우선 — 한 문서가 태그를 독점하면 그 문서의 조판만 재는 셈이 된다
        by_doc: dict[str, list] = collections.defaultdict(list)
        for c in cands:
            by_doc[c["doc"]].append(c)
        for lst in by_doc.values():
            rnd.shuffle(lst)
        take, i = [], 0
        while len(take) < per_tag:
            added = False
            for lst in by_doc.values():
                if i < len(lst):
                    take.append(lst[i]); added = True
                    if len(take) >= per_tag:
                        break
            if not added:
                break
            i += 1
        for c in take:
            key = (c["doc"], c["page"])
            if key not in seen:
                seen.add(key); picked.append(c)
    return picked, scans


def build(picked: list, out: pathlib.Path, variants: list[str]) -> list[dict]:
    import pdfplumber
    import pymupdf
    from PIL import Image
    from v1.utils.triage import load_config

    cfg = load_config()

    (out / "truth").mkdir(parents=True, exist_ok=True)
    manifest = []
    by_doc: dict[str, list] = collections.defaultdict(list)
    for c in picked:
        by_doc[c["path"]].append(c)

    for path, items in by_doc.items():
        doc = pymupdf.open(path)
        plumb = pdfplumber.open(path)
        stem = pathlib.Path(path).stem[:40]
        for c in items:
            pno = c["page"] - 1
            page = doc[pno]
            text = page.get_text("text").strip()
            tables, cells = _tables_html(plumb.pages[pno])
            blocks = _truth_blocks(page, cfg)
            tdir = out / "truth" / stem
            tdir.mkdir(parents=True, exist_ok=True)
            tp = tdir / f"p{c['page']:04d}.json"
            tp.write_text(json.dumps({
                "doc": c["doc"], "page": c["page"], "tags": c["tags"],
                "page_w": round(page.rect.width, 1), "page_h": round(page.rect.height, 1),
                "text": text, "blocks": blocks, "tables_html": tables,
                "table_cells": cells,
            }, ensure_ascii=False), encoding="utf-8")

            for vname in variants:
                cfgv = BENCH_VARIANTS[vname]
                zoom = cfgv["dpi"] / 72
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                img = _degrade(img, cfgv.get("angle"), cfgv.get("gamma"), cfgv.get("shadow"),
                               cfgv.get("blur"), cfgv.get("noise"), cfgv.get("shrink"))
                vd = out / "img" / vname / stem
                vd.mkdir(parents=True, exist_ok=True)
                ip = vd / f"p{c['page']:04d}.png"
                img.save(ip)
                manifest.append({
                    "set": "synthetic", "doc": c["doc"], "page": c["page"],
                    "variant": vname, "variant_tag": VARIANT_TAG[vname],
                    "tags": c["tags"], "image": str(ip.relative_to(out)),
                    "truth": str(tp.relative_to(out)),
                    "truth_chars": len(text), "dpi": cfgv["dpi"],
                    "size": [img.width, img.height],
                    # TEDS 는 정답 표가 있을 때만. 없으면 열 수 일관성으로 대체한다.
                    "teds_golden": bool(tables),
                    "table_cells": cells,
                })
        plumb.close()
        doc.close()
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/bench")
    ap.add_argument("--per-tag", type=int, default=12, help="태그별 최대 페이지")
    ap.add_argument("--variants", default=",".join(BENCH_VARIANTS))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--limit-docs", type=int, default=0)
    a = ap.parse_args()

    srcs = sorted(set(
        list((ROOT / "data/finished").glob("*.pdf"))
        + list((ROOT / "data/input").glob("*.pdf"))
        + list((ROOT / "data/input/gwanak").glob("*.pdf"))
    ))
    if a.limit_docs:
        srcs = srcs[:a.limit_docs]
    print(f"  원본 PDF {len(srcs)}건")

    picked, scans = collect(srcs, a.per_tag, a.seed)
    tagc = collections.Counter(t for c in picked for t in c["tags"])
    print(f"  합성 후보 {len(picked)}p · 태그 {dict(tagc)}")
    print(f"  실제 스캔(정답 없음) {len(scans)}p")

    out = pathlib.Path(a.out)
    variants = [v for v in a.variants.split(",") if v in BENCH_VARIANTS]
    manifest = build(picked, out, variants)

    (out / "manifest.json").write_text(json.dumps({
        "version": 1, "per_tag": a.per_tag, "seed": a.seed,
        "variants": variants,
        "synthetic": manifest,
        "real_scans": scans,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  합성 {len(manifest)}장 ({len(picked)}p × 변형 {len(variants)})")
    print(f"  TEDS 채점 가능: {sum(1 for m in manifest if m['teds_golden'])}장")
    print(f"  → {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
