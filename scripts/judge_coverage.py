"""별표3 ICD 보장판정 — "이 병(코드)이 이 담보의 보장범위인가?" (완결성의 coverage_scope 홉).

약관은 병명이 아니라 KCD 코드 범위로 보장을 정의한다(암=C00~C97+특정D). 그래서 판정 기준은
코드. 담보 특정성(C=일반암 / D00~D09=제자리암 / D37~D48=경계성)과 제외 우선을 반영해
3-값(보장/미보장/판정불가)을 낸다. 판정불가를 두는 게 precision-first(억지 판정 안 함).

- 별표3 '대상질병|분류번호' 표에서 담보별 코드범위 파싱(col0 병명 키워드로 담보 라벨).
- 코드 모델: (문자, 주, 부) 튜플. 단일 Cnn은 Cnn.0~Cnn.9 전체. 범위는 튜플 순서 비교.
- 병명→코드 매핑은 별도 계층(판정기는 코드만 받음 — 관심사 분리).

용법: python3 scripts/judge_coverage.py                 # 골든 채점
      python3 scripts/judge_coverage.py C50 암진단자금     # 단건 판정
"""
import os
import re
import sys
import glob
import json

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_coverage.jsonl")

# 분류표 col0 병명 키워드 → 담보
DISEASE_COV = [
    ("악성신생물", "암진단자금"),
    ("제자리암", "제자리암진단자금"),
    ("행동양식 불명", "경계성종양진단자금"),   # 경계성종양 = 행동양식 불명·미상 신생물
]


def _md(doc: str) -> str:
    path = next(p for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if doc in p)
    return open(path, encoding="utf-8").read()


def _code(s: str):
    """'C50.9' → ('C',50,9). 부번호 없으면 3번째는 None(카테고리 전체 의미)."""
    m = re.match(r'\s*([A-Z])(\d{1,2})(?:\.(\d))?', s)
    return (m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None) if m else None


def _bounds(tok: str):
    """토큰 → (lo, hi) 튜플. 'C00~C14' 범위 / 'C50' 카테고리 / 'D47.1' 단일."""
    parts = re.split(r'\s*[~\-]\s*', tok)
    if len(parts) == 2:
        lo, hi = _code(parts[0]), _code(parts[1])
        return (lo[0], lo[1], lo[2] or 0), (hi[0], hi[1], hi[2] if hi[2] is not None else 9)
    c = _code(tok)
    if c[2] is None:
        return (c[0], c[1], 0), (c[0], c[1], 9)      # 카테고리 Cnn = Cnn.0~Cnn.9
    return c, c


def _contains(code: str, tok: str) -> bool:
    c = _code(code)
    c = (c[0], c[1], c[2] or 0)
    lo, hi = _bounds(tok)
    return lo <= c <= hi


def coverage_ranges(doc: str) -> dict:
    """별표3 표들 → {담보: [코드토큰...]}. col0 병명으로 담보 라벨."""
    out = {}
    for ln in _md(doc).split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = s.strip("|").split("|")
        if len(cells) != 2:
            continue
        col0, col1 = cells[0], cells[1]
        cov = next((c for kw, c in DISEASE_COV if kw in col0), None)
        if not cov:
            continue
        toks = re.findall(r'[CD]\d{2}(?:\.\d)?(?:\s*[~\-]\s*[CD]\d{2}(?:\.\d)?)?', col1)
        if toks:
            out.setdefault(cov, []).extend(toks)
    return out


def judge(doc: str, code: str, coverage: str) -> dict:
    """(코드, 담보) → 3-값 판정 + 근거."""
    ranges = coverage_ranges(doc)
    my = ranges.get(coverage, [])
    hit = next((t for t in my if _contains(code, t)), None)
    if hit:
        return {"verdict": "보장", "evidence": f"{coverage} 범위 {hit} 포함"}
    # 다른 담보 범위에 들면 '미보장(해당 담보 아님)' — 담보 특정성
    for cov, toks in ranges.items():
        if cov == coverage:
            continue
        other = next((t for t in toks if _contains(code, t)), None)
        if other:
            return {"verdict": "미보장", "evidence": f"{code}는 {cov} 범위({other}) — {coverage} 아님"}
    return {"verdict": "판정불가", "evidence": f"{code} 어느 범위에도 없음 → RAG/전문가"}


def main():
    if len(sys.argv) > 2:                                 # 단건
        doc = next(p.split("/")[-1][:-3] for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if "다이렉트" in p)
        r = judge(doc, sys.argv[1], sys.argv[2])
        print(f"{sys.argv[1]} / {sys.argv[2]} → {r['verdict']} ({r['evidence']})")
        return

    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    ok = 0
    print(f"{'코드':<8}{'담보':<18}{'기대':<10}{'판정':<10}판정")
    print("-" * 58)
    for g in rows:
        r = judge(g["doc"], g["code"], g["coverage"])
        hit = (r["verdict"] == g["expected"])
        ok += hit
        print(f"{g['code']:<8}{g['coverage']:<18}{g['expected']:<10}{r['verdict']:<10}{'✅' if hit else '❌'}")
    print("-" * 58)
    print(f"정확도 {ok}/{len(rows)}  → 별표3 코드 보장판정(담보특정성·제외우선·판정불가)")


if __name__ == "__main__":
    main()
