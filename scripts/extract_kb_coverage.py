"""KB 암 계열 별표3 ICD 보장판정 코드셋 — "이 병(코드) 이 담보로 보장돼?" 결정론.

KB 암진단비는 별표3 악성신생물(암) 분류표를 참조하되 **유사암(기타피부암 C44·갑상선암 C73)을
제외**한다("암(유사암제외) = 암 − C44 − C73", 약관 제3조 명시). 이 제외 경계가 텍스트에 명확히
정의돼 있어 **범위 뺄셈으로 담보별 코드셋을 결정론 계산**할 수 있다(judge_coverage의 담보 특정성).

  암진단비   = 악성신생물 별표3 − {C44, C73}
  갑상선암   = {C73}          (중증갑상선암진단비 등)
  기타피부암 = {C44}          (유사암 소분류)

judge_coverage(코드,담보) 3-값(보장/미보장→리다이렉트/판정불가)이 이 코드셋으로 판정. 소스는 raw
md(별표3 표는 파이프 셀 보존). 유사암 제외가 없는 회사(다이렉트)는 기존 judge_coverage 그대로.

용법: python3 scripts/extract_kb_coverage.py            # 골든 채점(KB 암 판정)
"""
import os
import re
import glob
import json
import importlib.util

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_kb_coverage.jsonl")

_jc = importlib.util.spec_from_file_location("judge_coverage", os.path.join(HERE, "judge_coverage.py"))
jc = importlib.util.module_from_spec(_jc); _jc.loader.exec_module(jc)   # _contains 재사용

# 유사암 = 암진단비에서 제외되는 분류번호(약관 제3조 명시: 기타피부암 C44 · 갑상선암 C73)
_YUSA = {("C", 44): "기타피부암", ("C", 73): "갑상선암"}


def _cancer_tokens(doc: str) -> list[str]:
    """별표3 악성신생물(암) 분류표 셀에서 코드범위 토큰(C00~C14 … D47.5). 못 찾으면 []."""
    path = next(p for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if doc in p)
    for ln in open(path, encoding="utf-8"):
        if ln.strip().startswith("|") and "입술, 구강 및 인두의 악성신생물" in ln:
            col1 = ln.strip().strip("|").split("|")[1].replace("<br>", " ")
            return re.findall(r'[CD]\d{2}(?:\.\d)?(?:\s*[~\-]\s*[CD]\d{2}(?:\.\d)?)?', col1)
    return []


def _expand_c(tok: str) -> list[int]:
    """C-범주 토큰 → 카테고리 정수 리스트. 'C43~C44'→[43,44], 'C50'→[50]. C코드만(D는 그대로 유지)."""
    m = re.match(r'C(\d{2})(?:\s*[~\-]\s*C(\d{2}))?$', tok.replace(" ", ""))
    if not m:
        return []
    lo = int(m.group(1)); hi = int(m.group(2)) if m.group(2) else lo
    return list(range(lo, hi + 1))


def _collapse(cats: list[int]) -> list[str]:
    """정렬된 카테고리 정수 → 최소 범위 토큰. [43,45,46,47]→['C43','C45~C47']."""
    cats = sorted(cats)
    out, i = [], 0
    while i < len(cats):
        j = i
        while j + 1 < len(cats) and cats[j + 1] == cats[j] + 1:
            j += 1
        out.append(f"C{cats[i]:02d}" if i == j else f"C{cats[i]:02d}~C{cats[j]:02d}")
        i = j + 1
    return out


def coverage_ranges_kb(doc: str) -> dict:
    """{담보: [코드토큰...]} — 암진단비(유사암제외)·갑상선암·기타피부암. 범위 뺄셈으로 담보 특정성."""
    toks = _cancer_tokens(doc)
    if not toks:
        return {}
    c_cats, d_toks = [], []
    for t in toks:
        cats = _expand_c(t)
        if cats:
            c_cats += cats
        elif re.match(r'D', t.strip()):
            d_toks.append(t.strip())
    excl = {cat for (ltr, cat) in _YUSA}                       # {44, 73}
    cancer_cats = [c for c in c_cats if c not in excl]          # 암진단비 = 악성신생물 − 유사암
    out = {"암진단비": _collapse(cancer_cats) + d_toks}
    for (ltr, cat), cov in _YUSA.items():
        if cat in c_cats:
            out.setdefault(cov, []).append(f"{ltr}{cat}")
    return out


def judge(doc: str, code: str, coverage: str) -> dict:
    """(코드, 담보) → 3-값 판정. judge_coverage와 동일 규약(담보 범위 → 다른 담보 리다이렉트 → 판정불가)."""
    ranges = coverage_ranges_kb(doc)
    my = ranges.get(coverage, [])
    if any(jc._contains(code, t) for t in my):
        hit = next(t for t in my if jc._contains(code, t))
        return {"verdict": "보장", "redirect_coverage": None, "evidence": f"{coverage} 범위 {hit} 포함"}
    for cov, toks in ranges.items():
        if cov == coverage:
            continue
        other = next((t for t in toks if jc._contains(code, t)), None)
        if other:
            return {"verdict": "미보장", "redirect_coverage": cov,
                    "evidence": f"{code}는 {cov}({other}) — {coverage} 아님"}
    return {"verdict": "판정불가", "redirect_coverage": None, "evidence": f"{code} 어느 범위에도 없음 → RAG"}


def main():
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    ok = 0
    print(f"{'코드':<8}{'담보':<14}{'기대':<10}{'판정':<10}판정")
    print("-" * 58)
    for g in rows:
        r = judge(g["doc"], g["code"], g["coverage"])
        hit = (r["verdict"] == g["expected"]) and (g.get("redirect") in (None, r.get("redirect_coverage")))
        ok += hit
        print(f"{g['code']:<8}{g['coverage']:<14}{g['expected']:<10}{r['verdict']:<10}"
              f"{'✅' if hit else '❌ '+str(r.get('redirect_coverage'))}")
    print("-" * 58)
    print(f"정확도 {ok}/{len(rows)}  → KB 암 별표3 판정(유사암 제외 범위 뺄셈·담보 특정성)")


if __name__ == "__main__":
    main()
