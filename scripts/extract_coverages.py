"""담보 catalog 추출 — 소비자 "이 상품 뭐뭐 보장해?" + "X 보장 담보야?"(멤버십).

payout_rule에 흩어진 담보를 상품별 정규화 목록으로 모은다(① 보장의 '담보 존재' 축).
ICD 판정(judge_coverage)이 '내 병이 범위인가'를 본다면, 여기는 '이 상품이 무슨 담보를
가지나'를 본다. payout 표 없는 담보(제도성 등)는 안 잡힘 → RAG 폴백(precision-first).

용법: python3 scripts/extract_coverages.py                 # 골든 채점
      python3 scripts/extract_coverages.py 다이렉트 암진단자금   # 멤버십 단건
"""
import os
import re
import sys
import glob
import json
import importlib.util

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_catalog.jsonl")


def _load(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


lp = _load("load_payout")


def _norm(s: str) -> str:
    """담보명 정규화 — 괄호 원어(Resin 등)·<br> 제거, 공백 정리."""
    s = re.sub(r"\([^)]*\)", "", s.replace("<br>", " "))
    return re.sub(r"\s+", " ", s).strip()


def extract_coverages(doc: str) -> list:
    """상품의 보장성 담보 목록(정규화·중복제거). 현재 소스=payout_rule 담보."""
    raw = {_norm(r["coverage"]) for r in lp._rows_for(doc) if r.get("coverage")}
    by_key = {}                                            # 공백무시 dedup(가철성 의치=가철성의치)
    for c in raw:
        if not c:
            continue
        k = re.sub(r"\s", "", c)
        if k not in by_key or (" " in c and " " not in by_key[k]):   # 공백 있는 표기를 대표로(가독성)
            by_key[k] = c
    return sorted(by_key.values())


def is_covered(doc: str, keyword: str):
    """멤버십 — 키워드가 어느 담보에 속하나(부분일치). 없으면 None."""
    return next((c for c in extract_coverages(doc) if keyword in c), None)


def main():
    if len(sys.argv) > 2:                                  # 멤버십 단건
        doc = next(p.split("/")[-1][:-3] for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if sys.argv[1] in p)
        hit = is_covered(doc, sys.argv[2])
        print(f"{sys.argv[2]} → {'보장 담보 있음: '+hit if hit else '해당 담보 없음(→RAG)'}")
        return

    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    tot = hit = 0
    for r in rows:
        cat = extract_coverages(r["doc"])
        missing = [k for k in r["must_include"] if not any(k in c for c in cat)]
        tot += len(r["must_include"]); hit += len(r["must_include"]) - len(missing)
        ok = "✅" if not missing else f"❌ 빠짐={missing}"
        print(f"{r['doc'][:16]:<18} 담보 {len(cat)}개  필수 {len(r['must_include'])}  {ok}")
    print("-" * 50)
    print(f"필수담보 recall={hit/tot:.2f} ({hit}/{tot})  → 상품별 담보 catalog + 멤버십")


if __name__ == "__main__":
    main()
