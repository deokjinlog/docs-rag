"""계약조건 추출 — 소비자 "언제까지?" (청약철회·갱신·만기). 조 본문의 고정사실.

지급표(payout)와 달리 '성립·유지 관'의 조 본문에 흩어진 스칼라 고정사실. 특약은 준용이라
청약철회 등이 없으면 **NULL이 정답**(보통약관 소관) — resolution_note로 '왜 NULL'을 남긴다.
product 테이블의 cooling_off_days·is_renewable 컬럼을 채우는 소스.

용법: python3 scripts/extract_terms.py     # 골든 채점
"""
import os
import re
import glob
import json
import unicodedata

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_terms.jsonl")


def _md(doc: str) -> str:
    processed = os.path.join(HERE, "..", "data/output/processed", doc, "clean.md")   # processed 우선(raw 폴백)
    if os.path.exists(processed):
        return open(processed, encoding="utf-8").read()
    path = next(p for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if doc in p)
    return open(path, encoding="utf-8").read()


def extract_terms(doc: str) -> dict:
    """조 본문에서 청약철회·갱신·만기 고정사실 추출. 못 뽑으면 NULL(+준용 사유)."""
    md = _md(doc)
    t = {"cooling_off_days": None, "is_renewable": None,
         "renewal_cycle_years": None, "term_years": None, "resolution_note": None}

    # 청약철회 — '철회' 문맥 안의 'N일 이내'만(엉뚱한 N일 이내 배제). 표준: "받은 날부터 15일 이내"
    # 주의: 복합약관은 문서 전체에 철회 언급이 많아(반환기일 "철회 접수 후 N일 이내"·특약별) 이 느슨한
    # 패턴이 반환기일을 오탐할 수 있다(회사미상 수술비=3일 반환 오추출 실측). 청약철회 조-스코프 정밀화 +
    # ground-truth 골든(문서별 실제 N일)이 회사미상 terms 적재의 선결 — @docs/STATUS.md.
    m = re.search(r'철회[^\n]{0,50}?(\d+)\s*일\s*이내', md) or \
        re.search(r'(\d+)\s*일\s*이내[^\n]{0,30}?철회', md)
    t["cooling_off_days"] = int(m.group(1)) if m else None
    if t["cooling_off_days"] is None:
        t["resolution_note"] = "청약철회 미기재 → 특약이면 보통약관 준용 소관"

    # 갱신 — '갱신형'/'자동갱신'/'N년마다 갱신' 중 하나라도
    t["is_renewable"] = bool(re.search(r'갱신\s*형|자동\s*갱신|\d+\s*년\s*마다\s*갱신', md))

    cyc = re.search(r'(\d+)\s*년\s*마다\s*갱신', md)          # 갱신 주기
    t["renewal_cycle_years"] = int(cyc.group(1)) if cyc else None

    # 만기 — '보험기간은 N년만기' 선언만. KB 간편건강보험은 갱신구조 설명에 예시 만기가 흩어져
    # ("예 시 48세가 5년만기로 80세까지 갱신") 홑 'N년만기'로 잡으면 예시값을 상품 만기로 오추출(FP).
    # 보험기간 앵커 + 예시(예/피보험자) 네거티브 가드로 라이나 '보험기간은 10년만기'만 남기고 예시 배제.
    t["term_years"] = None
    for m in re.finditer(r'보험기간[은는:]?\s*(\d+)\s*년\s*만기', md):
        if any(k in md[max(0, m.start() - 15):m.start()] for k in ("예", "피보험자")):
            continue                                          # 예시 문맥 → 상품 만기 아님(precision-first)
        t["term_years"] = int(m.group(1))
        break
    return t


# ── 골든 채점 ──────────────────────────────────────────────────────
_CACHE = {}


def predict(doc: str, field: str):
    if doc not in _CACHE:
        _CACHE[doc] = extract_terms(doc)
    return _CACHE[doc].get(field)


def _norm(s):
    if s is None:
        return None
    return unicodedata.normalize("NFKC", str(s)).replace(" ", "").lower()


def _judge(gold, pred):
    g, p = _norm(gold), _norm(pred)
    if g is not None and p == g:     return "TP", "✅ TP"
    if g is not None and p is None:  return "FN", "❌ FN(놓침)"
    if g is not None and p != g:     return "FP", f"❌ FP(틀림→{pred})"
    if g is None and p is None:      return "TN", "✅ TN(맞게비움)"
    return "FP", "❌ FP(헛짚음)"


def main():
    from collections import Counter
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    C = Counter()
    print(f"{'문서':<14}{'필드':<22}{'정답':<8}{'추출':<8}판정")
    print("-" * 62)
    for r in rows:
        pred = predict(r["doc"], r["field"])
        cat, v = _judge(r["expected"], pred)
        C[cat] += 1
        print(f"{r['doc'][:12]:<14}{r['field']:<22}{str(r['expected']):<8}{str(pred):<8}{v}")
    print("-" * 62)
    rec = C["TP"] / (C["TP"] + C["FN"]) if (C["TP"] + C["FN"]) else 1.0
    prec = C["TP"] / (C["TP"] + C["FP"]) if (C["TP"] + C["FP"]) else 1.0
    print(f"recall={rec:.2f} precision={prec:.2f} "
          f"(TP{C['TP']} FN{C['FN']} FP{C['FP']} TN{C['TN']})  준용 NULL={C['TN']}  FP={C['FP']}")


if __name__ == "__main__":
    main()
