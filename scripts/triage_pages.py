"""페이지 분류(triage) — 어느 파싱 경로로 보낼지 **규칙 기반**으로 정한다. (캐스케이드 M1)

지금 파이프라인은 모든 PDF 를 ODL 한 경로로 보낸다. 네이티브 PDF 뿐이라 지금은 맞지만,
스캔본 특약이나 팩스 이미지가 들어오면 조용히 빈 텍스트가 나온다. 그걸 막는 보험이자,
비싼 경로(VL, 이미지당 ~11초)를 **필요한 페이지에만** 쓰기 위한 라우터다.

**왜 LLM/VLM 분류기가 아닌가** — 98% 정확도 분류기도 1,000건이면 20건을 엉뚱한 경로로
보낸다. 규칙은 틀리는 방식이 예측 가능해서 디버깅이 되고, 페이지당 수 ms 로 끝난다.
애매한 것만 위 티어로 올리는 게 이 프로젝트의 precision-first 와 같은 규율이다.

**이 스크립트의 역할은 아직 라우팅이 아니라 관측**이다. 임계치를 감으로 정하지 않기 위해
먼저 **분포를 찍는다**. 코퍼스가 전부 NATIVE 로 나오면 그것대로 결론이다 —
"라우터는 보험이고, VL 전량 처리는 애초에 설계에 없다"가 데이터로 확정된다.

신호 (전부 PyMuPDF, 모델 없음):
    char_count     텍스트 레이어 글자 수          0 에 가까우면 이미지 페이지
    image_cover    이미지 bbox 합 / 페이지 면적   높으면 스캔본
    garbage_ratio  U+FFFD·제어문자·분리자모 비율   "레이어는 있는데 쓰레기"
    table_score    표 개수·셀 수                 표 밀도
    column_count   텍스트 블록 x 클러스터 수      다단 여부
    rotation       페이지 회전                   회전 스캔

용법:
    python3 scripts/triage_pages.py <PDF...>            # 페이지별 판정 + 분포
    python3 scripts/triage_pages.py --all               # data/input 전체
    python3 scripts/triage_pages.py --all --csv out.csv # 원신호까지 CSV 로
"""
import argparse
import csv
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── 임계치 ────────────────────────────────────────────────────────────────────
# ⚠ **잠정값이다.** 분포를 보기 전에 정한 감이라, --csv 로 실제 분포를 찍고 조정한다.
# 순서가 중요하다 — 임계치를 먼저 고민하면 데이터가 아니라 취향으로 정하게 된다.
CHAR_MIN = 50          # 이 미만이면 텍스트 레이어가 사실상 없음
IMAGE_COVER_HIGH = 0.80
GARBAGE_MAX = 0.15     # 이 초과면 레이어가 있어도 못 믿음
TABLE_SCORE_HIGH = 2   # 표 2개 이상이면 복잡
COLUMN_GAP = 60        # x 클러스터를 가르는 간격(px) — reconstruct_reading_order 와 같은 값

# 분리 자모(조합 안 된 한글) — 추출 실패의 전형적 흔적
_JAMO = re.compile(r"[ᄀ-ᇿ㄰-㆏]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f�]")


def _garbage_ratio(text: str) -> float:
    """레이어는 있는데 못 쓰는 경우를 잡는다.

    한글 PDF 에서 흔한 실패는 '텍스트가 없다'가 아니라 **'있는데 깨졌다'** 다 —
    CID 폰트나 ToUnicode 누락이면 U+FFFD 나 분리 자모가 쏟아진다. char_count 만 보면
    이걸 NATIVE 로 오분류해 조용히 쓰레기를 색인한다.
    """
    if not text:
        return 0.0
    bad = len(_CTRL.findall(text)) + len(_JAMO.findall(text))
    return bad / len(text)


def _column_count(page) -> int:
    """텍스트 블록의 x0 를 클러스터링해 단 수를 센다."""
    xs = sorted(round(b[0]) for b in page.get_text("blocks") if b[6] == 0 and b[4].strip())
    if not xs:
        return 0
    cols, prev = 1, xs[0]
    for x in xs[1:]:
        if x - prev > COLUMN_GAP:
            cols += 1
        prev = x
    return cols


def analyze_page(page) -> dict:
    import pymupdf  # noqa: F401  (호출부에서 이미 import 됨을 보장)
    text = page.get_text("text")
    area = abs(page.rect.width * page.rect.height) or 1.0
    # 이미지 커버리지 — 합집합이 아니라 합(중복 가능)이라 1.0 을 넘을 수 있어 clamp
    img_area = 0.0
    for info in page.get_images(full=True):
        try:
            for r in page.get_image_rects(info[0]):
                img_area += abs(r.width * r.height)
        except Exception:
            pass
    try:
        tables = len(page.find_tables().tables)
    except Exception:
        tables = 0
    return {
        "char_count": len(text.strip()),
        "image_cover": round(min(img_area / area, 1.0), 3),
        "garbage_ratio": round(_garbage_ratio(text), 4),
        "table_score": tables,
        "column_count": _column_count(page),
        "rotation": page.rotation,
    }


def classify(sig: dict) -> str:
    """4-값 판정. 순서가 곧 우선순위다 — 싼 경로를 기본으로 두고 신호가 있을 때만 격상."""
    # ① 레이어가 있어도 못 믿는 경우를 **먼저** 본다. char_count 로 먼저 통과시키면
    #    깨진 텍스트가 NATIVE 로 새어 조용히 쓰레기를 색인한다.
    if sig["char_count"] >= CHAR_MIN and sig["garbage_ratio"] > GARBAGE_MAX:
        return "NATIVE_SUSPECT"
    if sig["char_count"] >= CHAR_MIN:
        return "NATIVE"
    # ② 텍스트가 없다 → 스캔 계열. 복잡도로 OCR / VL 을 가른다.
    complex_ = (sig["table_score"] >= TABLE_SCORE_HIGH or sig["column_count"] >= 2
                or sig["rotation"] % 180 != 0)
    return "SCAN_COMPLEX" if complex_ else "SCAN_SIMPLE"


ROUTE = {"NATIVE": "ODL", "NATIVE_SUSPECT": "OCR 재추출 후 비교",
         "SCAN_SIMPLE": "OCR", "SCAN_COMPLEX": "VL"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="*")
    ap.add_argument("--all", action="store_true", help="data/input 의 모든 PDF")
    ap.add_argument("--csv", help="페이지별 원신호를 CSV 로")
    ap.add_argument("--limit", type=int, default=0, help="문서당 최대 페이지(빠른 확인용)")
    a = ap.parse_args()

    try:
        import pymupdf
    except ImportError:
        print("pymupdf 필요:  uv run --no-project --with pymupdf python scripts/triage_pages.py ...",
              file=sys.stderr)
        return 2

    paths = [pathlib.Path(p) for p in a.pdfs]
    if a.all:
        paths = sorted((ROOT / "data/input").glob("*.pdf"))
    if not paths:
        ap.error("PDF 를 주거나 --all")

    rows, per_doc = [], {}
    for p in paths:
        try:
            doc = pymupdf.open(p)
        except Exception as e:
            print(f"  ⚠ {p.name}: {type(e).__name__}", file=sys.stderr)
            continue
        n = min(len(doc), a.limit) if a.limit else len(doc)
        cnt = collections.Counter()
        for i in range(n):
            sig = analyze_page(doc[i])
            verdict = classify(sig)
            cnt[verdict] += 1
            rows.append({"doc": p.name, "page": i + 1, "verdict": verdict, **sig})
        per_doc[p.name] = (cnt, n)
        doc.close()

    print(f"  {'문서':<46}{'p':>6}  {'NATIVE':>8}{'SUSPECT':>9}{'SIMPLE':>8}{'COMPLEX':>9}")
    print("  " + "─" * 88)
    total = collections.Counter()
    tot_pages = 0
    for name, (cnt, n) in per_doc.items():
        total += cnt
        tot_pages += n
        print(f"  {name[:44]:<46}{n:>6}  {cnt['NATIVE']:>8}{cnt['NATIVE_SUSPECT']:>9}"
              f"{cnt['SCAN_SIMPLE']:>8}{cnt['SCAN_COMPLEX']:>9}")
    print("  " + "─" * 88)
    print(f"  {'합계':<46}{tot_pages:>6}  {total['NATIVE']:>8}{total['NATIVE_SUSPECT']:>9}"
          f"{total['SCAN_SIMPLE']:>8}{total['SCAN_COMPLEX']:>9}")
    if tot_pages:
        print()
        for v in ("NATIVE", "NATIVE_SUSPECT", "SCAN_SIMPLE", "SCAN_COMPLEX"):
            c = total[v]
            print(f"    {v:<16}{c:>6}p  {c / tot_pages:6.1%}   → {ROUTE[v]}")
        # 비싼 경로 예산 — 설계 판단의 핵심 숫자
        vl = total["SCAN_COMPLEX"]
        print(f"\n  VL 예산: {vl}p × ~11s = {vl * 11 / 60:.1f}분"
              f"{'  (전량 VL 대비 압도적 절감)' if vl < tot_pages * 0.1 else '  ⚠ 비중이 높다 — 설계 재검토'}")

    if a.csv:
        out = pathlib.Path(a.csv)
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n  원신호 CSV: {out}  ({len(rows)}행) — 임계치는 이 분포를 보고 정한다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
