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
er = _load("extract_exclusion_reasons")
st = _load("stage")                                   # silver resolver(clean.md·clauses.jsonl 캐시)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_completeness.jsonl")

# 담보 키워드 → 소속 문서
COV_DOC = {
    "중환자실": "라이나_중환자실입원특약",
    "레진": "New치아보험_약관", "아말감": "New치아보험_약관", "인레이": "New치아보험_약관",
    "제자리암": "다이렉트늘안심입원비보험_약관", "암진단자금": "다이렉트늘안심입원비보험_약관",
    "소득보장": "라이나_소득보장수술특약",
}
EXCL_KW = ("지급하지 않", "지급하지아니", "보상하지 않", "보장하지 않", "면책")


def _exclusions(doc: str) -> list:
    """강제첨부용 — 문서의 면책 조 제목(제목규칙). 담보 물어봐도 항상 붙인다."""
    clauses = st.doc_clauses(doc)                     # clauses.jsonl 캐시(없으면 파싱 폴백)
    return [c["title"] for c in clauses if any(k in c["title"] for k in EXCL_KW)][:3]


def _redirect_rate(doc: str, cov_kw: str, it: dict) -> str:
    """리다이렉트 담보(예: 제자리암진단자금)의 지급률 — 경과기간 반영."""
    rows = [r for r in qp._all_rows() if cov_kw[:4] in (r.get("coverage") or "")]
    for r in rows:
        if it.get("period_bucket") and r.get("period_bucket") == it["period_bucket"]:
            return f"{r['rate_pct']}%"
    return f"{rows[0]['rate_pct']}%(경과기간별)" if rows else "별표 참조"


def _reconcile(doc: str, code: str, cov_name: str, v: dict, it: dict, payout_row) -> str:
    """사실 화해 — 보장판정이 payout을 게이팅. 미보장이면 실제 담보로 리다이렉트(제외 우선)."""
    if v["verdict"] == "보장":
        rate = payout_row.get("rate_pct") if payout_row else "?"
        return f"{code}는 {cov_name} 보장 → 지급 {rate}%"
    if v["verdict"] == "미보장" and v.get("redirect_coverage"):
        rc = v["redirect_coverage"]
        return f"{code}는 {cov_name} 대상 아님(미보장) → 실제 '{rc}' 담보로 {_redirect_rate(doc, rc, it)}"
    return f"{code} 판정불가 → 코드 확인/전문가"


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
        tm = et.extract_terms(doc)                       # 준용/계약조건
        is_rider = tm.get("cooling_off_days") is None    # 청약철회 미기재 = 준용 특약 신호

        ex = _exclusions(doc)                            # ④ 면책 강제첨부(담보별 고유는 특약이 자체 보유)
        if ex:
            reasons = er.extract_exclusion_reasons(doc)  # "뭐가 안 돼요?"의 실제 사유 목록
            el["exclusion"] = {"clause": ex, "reasons": reasons}
            tags.add("exclusion")
            if reasons:
                tags.add("exclusion_reasons")
            if is_rider:                                 # 특약에 '없는' 표준 공통면책만 준용 대상으로 짚음
                missing = sorted({"고의", "전쟁내란", "임신출산", "위험활동", "직업위험"} - set(reasons))
                el["exclusion_pending"] = f"보통약관 공통면책 준용 소관(특약 미기재): {', '.join(missing)} — corpus 미확보 시 확인"
                tags.add("exclusion_common_pending")

        if tm.get("resolution_note"):
            el["resolution"] = tm["resolution_note"]
            tags.add("resolution")
        if tm.get("cooling_off_days") is not None:
            el["cooling_off"] = f"청약철회 {tm['cooling_off_days']}일"
            tags.add("cooling_off")

        rng = jc.coverage_ranges(doc)                    # ① 보장범위 (별표3 ICD 판정 홉)
        cov_name = next((c for c in rng if it.get("coverage") and it["coverage"] in c), None)
        if cov_name:
            code = re.search(r'[A-Z]\d{2}(?:\.\d)?', q)  # 질문에 코드 있으면 특정 판정(C/D 외 Z 등도)
            if code:
                v = jc.judge(doc, code.group(), cov_name)
                el["coverage_scope"] = f"{code.group()} → {v['verdict']} ({v['evidence']})"
                tags.add("coverage_scope")
                el["final"] = _reconcile(doc, code.group(), cov_name, v, it, row)   # 사실 화해
                el["reconcile"] = {"verdict": v["verdict"], "redirect": v.get("redirect_coverage")}
                tags.add("reconciled")
            else:
                el["coverage_scope"] = f"보장범위는 별표3 코드 기준({cov_name}) — 코드 입력 시 보장/미보장/판정불가"
                tags.add("coverage_scope")

    return {"question": q, "doc": doc, "elements": el, "tags": tags}


# ── 완결성 골든 채점 ────────────────────────────────────────────────
def format_answer(a: dict) -> str:
    """조립된 구조화 팩트 → 소비자용 답변 합성(결정론 템플릿). 근거는 element별 골든 검증."""
    el = a["elements"]
    out = []
    if "final" in el:                                       # 보장 여부(화해된 결론) 우선
        out.append(f"• 보장: {el['final']}")
    elif "coverage_scope" in el:
        out.append(f"• 보장범위: {el['coverage_scope']}")
    if "payout" in el:
        out.append(f"• 지급: {el['payout']}")
    if "exclusion" in el and el["exclusion"].get("reasons"):
        out.append(f"• 면책 사유: {', '.join(el['exclusion']['reasons'])}")
    if "exclusion_pending" in el:
        out.append(f"• 주의: {el['exclusion_pending']}")
    if "cooling_off" in el:
        out.append(f"• {el['cooling_off']}")
    if "resolution" in el:
        out.append(f"• {el['resolution']}")
    return "\n".join(out) if out else "관련 정보를 찾지 못했습니다(→RAG)."


def _score_reconcile():
    """사실 화해 채점 — 보장판정 verdict + 리다이렉트 담보가 맞나."""
    g = os.path.join(HERE, "..", "data", "eval", "golden_reconcile.jsonl")
    rows = [json.loads(l) for l in open(g, encoding="utf-8") if l.strip()]
    ok = 0
    print(f"{'질문':<30}{'기대판정':<10}{'판정':<10}{'리다이렉트':<16}판정")
    print("-" * 78)
    for r in rows:
        a = assemble_answer(r["question"])
        rc = a["elements"].get("reconcile", {})
        hit = (rc.get("verdict") == r["expected_verdict"]
               and (rc.get("redirect") or None) == (r.get("expected_redirect") or None))
        ok += hit
        print(f"{r['question'][:28]:<30}{r['expected_verdict']:<10}"
              f"{str(rc.get('verdict')):<10}{str(rc.get('redirect') or '-'):<16}{'✅' if hit else '❌'}")
    print("-" * 78)
    print(f"정확도 {ok}/{len(rows)}  → 보장판정이 payout을 게이팅/리다이렉트(제외 우선)")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--reconcile":
        _score_reconcile()
        return
    if len(sys.argv) > 1:                                 # 단건 조립 데모
        a = assemble_answer(sys.argv[1])
        print(f"질문: {a['question']}  (문서: {a['doc']})")
        for k, v in a["elements"].items():
            print(f"  [{k}] {v}")
        print(f"  태그: {sorted(a['tags'])}")
        print("\n─ 합성 답변 ─")
        print(format_answer(a))
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
