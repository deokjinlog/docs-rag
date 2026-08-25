"""PDF→md 교차검증 — 골든 값이 원본 PDF 텍스트층에 실재하나 (독립 추출기 pymupdf).

**순환을 깨는 법**: 골든은 ODL이 뽑은 md를 보고 라벨됐다. 그래서 "md의 오류"는 골든도 같이
틀릴 수 있다(같은 소스). 여기선 **pymupdf로 PDF 텍스트층을 직접** 읽어(ODL과 완전히 다른 엔진)
골든 값이 거기 실재하는지 본다. 두 독립 추출기가 **일치**하면 그 값은 PDF에 실재한다고 볼 근거가
강해진다(cross-engine consensus). **불일치**면 사람이 그 페이지를 렌더해 눈으로 확정할 후보다.

즉 이 지표는 사람눈을 대체하는 게 아니라, **사람이 볼 곳(불일치)을 좁혀** 소량의 사람눈을 전체로
확장한다. 디지털 PDF(텍스트층)라 본문·표의 코드/숫자는 near-lossless로 뽑힌다.

용법: python3 scripts/verify_extraction_vs_pdf.py            # 이번 세션 골든 교차검증
"""
import os
import re
import glob
import json
import unicodedata

HERE = os.path.dirname(__file__)
PDF_DIRS = ["data/finished", "data/input", "data/output/raw"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


def _find_pdf(doc: str):
    key = _norm(doc.replace("_약관", "").split("(")[0])[:12]
    for d in PDF_DIRS:
        for p in glob.glob(os.path.join(HERE, "..", d, "*.pdf")):
            if key in _norm(os.path.basename(p)):
                return p
    return None


_TEXT: dict = {}


def pdf_text(doc: str):
    """원본 PDF 전체 텍스트층(정규화). PDF 없으면 None. pymupdf=ODL과 독립 엔진."""
    if doc not in _TEXT:
        import pymupdf
        p = _find_pdf(doc)
        if not p:
            _TEXT[doc] = None
        else:
            d = pymupdf.open(p)
            _TEXT[doc] = _norm("".join(pg.get_text() for pg in d))
            d.close()
    return _TEXT[doc]


def _probe(row: dict, kind: str):
    """골든 행 → 검증 그룹 리스트. 그룹끼리 AND, 그룹 안 대안끼리 OR(표기 변이 흡수). None=대조 스킵."""
    e = row.get("expected")
    if kind == "terms":
        if row.get("field") == "cooling_off_days" and e:
            return [[f"{e}일이내"], ["철회"]]                              # 15일 이내 그리고 철회 문맥
        if row.get("field") == "is_renewable" and e is True:
            return [["갱신형"]]
    if kind == "waiting":
        if row.get("field") == "waiting_days" and e:
            return [[f"{e}일간보장제외"]]
        if row.get("field") == "reduction_rate_pct" and e:
            return [[f"1년간보험금{e}%지급", f"1년간가입금액{e}%지급"]]     # 보험금/가입금액 표기 변이
        if row.get("field") == "sub_rate_pct" and e:
            return [[f"90일미만{e}%"]]
    if kind == "kb_coverage":
        return [[_norm(row["code"])]]                                    # 코드가 별표3에 실재하나
    return None


def verify(golden: str, kind: str):
    rows = [json.loads(l) for l in open(os.path.join(HERE, "..", "data/eval", golden), encoding="utf-8") if l.strip()]
    found = miss = skip = nopdf = 0
    misses = []
    for r in rows:
        if r["expected"] in (None, ""):                                  # TN(맞게 비움)은 대조 대상 아님
            skip += 1; continue
        txt = pdf_text(r["doc"])
        if txt is None:
            nopdf += 1; continue
        groups = _probe(r, kind)
        if not groups:                                                    # probe 미정의 필드 → 대조 스킵
            skip += 1; continue
        hit = all(any(_norm(alt) in txt for alt in g) for g in groups)    # 그룹 AND · 대안 OR
        if hit:
            found += 1
        else:
            miss += 1
            misses.append(f"{r['doc'][:12]}·{r.get('code') or r.get('field')}={r['expected']}")
    n = found + miss
    rate = found / n if n else 1.0
    print(f"■ {golden:<26} PDF일치 {found}/{n} = {rate:.0%}"
          f"{'  (PDF없음 '+str(nopdf)+')' if nopdf else ''}{'  TN제외 '+str(skip) if skip else ''}")
    if misses:
        print(f"   ⚠ 사람 확인 후보(불일치): {misses[:6]}")
    return found, n, nopdf


def main():
    print("PDF→md 교차검증 — 독립 추출기(pymupdf 텍스트층) vs 골든 값")
    print("=" * 68)
    tf = tn = tno = 0
    for gf, kind in [("golden_terms.jsonl", "terms"),
                     ("golden_waiting.jsonl", "waiting"),
                     ("golden_kb_coverage.jsonl", "kb_coverage")]:
        f, n, no = verify(gf, kind)
        tf += f; tn += n; tno += no
    print("-" * 68)
    print(f"합계 PDF 텍스트층 일치 {tf}/{tn} = {tf/tn if tn else 1:.0%}"
          f"  → 100% 아니면 그 항목만 사람이 렌더+확인(사람눈 최소화, 대체 아님)")
    if tno:
        print(f"  (PDF 미보유 {tno}건 — 특약 원본 PDF는 별도 보관, 복합약관 부모 PDF에 포함)")


if __name__ == "__main__":
    main()
