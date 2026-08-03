"""답변 조립기 + 완결성 게이트 — 소비자 질문을 '완결 답변 계약'대로 조립.

완벽한 답은 한 값이 아니라 조립이다: 특약·보통약관·별표에 흩어진 조각을 준용·강제첨부로
해소해 모은다. 지급률만 답하고 면책을 빠뜨리면 소비자가 손해 → 완결성 게이트로 '필수 요소
누락'을 잡는다. 엣지(payout·terms·면책·준용)는 이미 있고, 이 파일은 그걸 질문유형별로 엮는 조립기.

엣지 소스(오프라인 대역):
  - payout   : query_payout.answer  (지급률·감액·한도)
  - 면책     : parse_clauses 제목규칙 (강제첨부)
  - 준용/조건: extract_terms.resolution_note (특약→보통약관 소관)

용법: python3 scripts/assemble_answer.py                     # 완결성 골든 채점
      python3 scripts/assemble_answer.py "중환자실 재해 아닌 병 입원 얼마?"   # 단건 조립
"""
import os
import re
import sys
import glob
import json
import importlib.util

HERE = os.path.dirname(__file__)


def _load(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


qp = _load("query_payout")
et = _load("extract_terms")
pc = _load("parse_clauses")
jc = _load("judge_coverage")
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_completeness.jsonl")

# 담보 키워드 → 소속 문서
COV_DOC = {
    "중환자실": "라이나_중환자실입원특약",
    "레진": "New치아보험_약관", "아말감": "New치아보험_약관", "인레이": "New치아보험_약관",
    "제자리암": "다이렉트늘안심입원비보험_약관", "암진단자금": "다이렉트늘안심입원비보험_약관",
    "소득보장": "라이나_소득보장수술특약",
}
EXCL_KW = ("지급하지 않", "지급하지아니", "보상하지 않", "보장하지 않", "면책")


def _md(doc: str) -> str:
    path = next(p for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if doc in p)
    return open(path, encoding="utf-8").read()


def _exclusions(doc: str) -> list:
    """강제첨부용 — 문서의 면책 조 제목(제목규칙). 담보 물어봐도 항상 붙인다."""
    clauses = pc.parse_clauses(_md(doc), "X")
    return [c["title"] for c in clauses if any(k in c["title"] for k in EXCL_KW)][:3]


def assemble_answer(q: str) -> dict:
    """질문 → 엣지 순회 조립 → {elements, tags}. 완결성은 골든이 required로 채점."""
    it = qp._intent(q)
    doc = COV_DOC.get(it.get("coverage"))
    el, tags = {}, set()

    row = qp.answer(q)                                    # ② 얼마 (+감액·한도)
    if row:
        el["payout"] = qp._fmt(row)
        tags.add("payout_rate")
        if row.get("period_bucket") or row.get("reduction_rate_pct"):
            tags.add("reduction")
        if row.get("limit_days"):
            tags.add("limit")

    if doc:
        ex = _exclusions(doc)                            # ④ 면책 강제첨부
        if ex:
            el["exclusion"] = ex
            tags.add("exclusion")
        tm = et.extract_terms(doc)                       # 준용/계약조건
        if tm.get("resolution_note"):
            el["resolution"] = tm["resolution_note"]
            tags.add("resolution")
        if tm.get("cooling_off_days") is not None:
            el["cooling_off"] = f"청약철회 {tm['cooling_off_days']}일"
            tags.add("cooling_off")

        rng = jc.coverage_ranges(doc)                    # ① 보장범위 (별표3 ICD 판정 홉)
        cov_name = next((c for c in rng if it.get("coverage") and it["coverage"] in c), None)
        if cov_name:
            code = re.search(r'[CD]\d{2}(?:\.\d)?', q)   # 질문에 코드 있으면 특정 판정
            if code:
                v = jc.judge(doc, code.group(), cov_name)
                el["coverage_scope"] = f"{code.group()} → {v['verdict']} ({v['evidence']})"
            else:
                el["coverage_scope"] = f"보장범위는 별표3 코드 기준({cov_name}) — 코드 입력 시 보장/미보장/판정불가"
            tags.add("coverage_scope")

    return {"question": q, "doc": doc, "elements": el, "tags": tags}


# ── 완결성 골든 채점 ────────────────────────────────────────────────
def main():
    if len(sys.argv) > 1:                                 # 단건 조립 데모
        a = assemble_answer(sys.argv[1])
        print(f"질문: {a['question']}  (문서: {a['doc']})")
        for k, v in a["elements"].items():
            print(f"  [{k}] {v}")
        print(f"  태그: {sorted(a['tags'])}")
        return

    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    total_req = total_hit = perfect = 0
    print(f"{'질문':<34}{'필수요소':<30}{'누락':<16}완결?")
    print("-" * 92)
    for g in rows:
        a = assemble_answer(g["question"])
        req = set(g["required"])
        missing = req - a["tags"]
        total_req += len(req); total_hit += len(req & a["tags"])
        perfect += (not missing)
        print(f"{g['question'][:32]:<34}{','.join(sorted(req)):<30}"
              f"{(','.join(sorted(missing)) or '-'):<16}{'✅' if not missing else '❌'}")
    print("-" * 92)
    rec = total_hit / total_req if total_req else 1.0
    print(f"필수요소 recall={rec:.2f}  완결 답변 {perfect}/{len(rows)}  "
          f"→ '면책 빠뜨림' 같은 불완전을 게이트가 차단")


if __name__ == "__main__":
    main()
