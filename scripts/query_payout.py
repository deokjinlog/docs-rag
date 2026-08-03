"""payout SQL 경로 — "얼마 받아요/언제부터 온전히?" 질의를 payout_rule SELECT로 결정론 답변.

3경로 라우터(SQL/RAG/fetch)의 SQL 경로 프로토타입. 질의에서 (담보·원인·연령·경과기간) 의도를
규칙으로 뽑아 payout_rule을 필터 → 결정론 값 반환. RAG처럼 확률적 해석이 아니라 '값을 집어온다'.

DB 없이 검증되게 payout_rule을 in-memory(load_payout 결과)로 대역. 실서비스는 이 필터를
`SELECT * FROM payout_rule WHERE ...`로 교체하면 그대로 라우터에 꽂힌다.

용법: python3 scripts/query_payout.py            # QA 골든 채점
      python3 scripts/query_payout.py "중환자실 입원하면 하루 얼마?"   # 단건 질의
"""
import os
import re
import sys
import json
import importlib.util

HERE = os.path.dirname(__file__)


def _mod(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


lp = _mod("load_payout")
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_payout_qa.jsonl")

# 담보 키워드 — 질의어 ↔ payout_rule.coverage 매칭용
COV_KW = ["중환자실", "레진", "아말감", "인레이", "제자리암", "암진단자금", "소득보장"]


_ROWS = None


def _all_rows():
    """payout_rule 대역(전 문서 규칙). 실서비스는 SELECT로 교체. 1회 캐시."""
    global _ROWS
    if _ROWS is None:
        _ROWS = []
        for doc in lp.DOC_PID:
            _ROWS += lp._rows_for(doc)
    return _ROWS


def _intent(q: str) -> dict:
    """질의에서 (담보·원인·연령·경과기간) 의도 추출 — 규칙 기반."""
    it = {}
    for kw in COV_KW:
        if kw in q:
            it["coverage"] = kw
            break
    if re.search(r"질병|병으로|아파", q):        it["cause"] = "질병"
    elif re.search(r"재해|상해|다쳐|사고", q):    it["cause"] = "상해"
    if re.search(r"15세\s*이상|성인", q):         it["age_band"] = "15세이상"
    elif re.search(r"15세\s*미만|어린|아동", q):  it["age_band"] = "15세미만"
    if re.search(r"90일\s*(이내|안|이하|전)", q):                    it["period_bucket"] = "90일이하"
    elif re.search(r"1년\s*(지나|이상|후|넘)", q):                    it["period_bucket"] = "1년이상"
    elif re.search(r"90일.*1년|1년.*90일|반년|몇\s*달", q):          it["period_bucket"] = "90일초과1년미만"
    return it


def answer(q: str) -> dict | None:
    """질의 → payout_rule SELECT(대역) → 결정론 결과 1건."""
    it = _intent(q)
    hits = []
    for r in _all_rows():
        if "coverage" in it and it["coverage"] not in (r.get("coverage") or ""):
            continue
        skip = False
        for k in ("cause", "age_band", "period_bucket"):    # 원인·연령·경과기간은 하드 필터
            if k in it and (r.get(k) or None) != it[k]:
                skip = True
                break
        if not skip:
            hits.append(r)
    if not hits:
        return None

    def _score(r):
        toks = [t for t in re.split(r"[\s()/]+", r.get("coverage") or "") if len(t) >= 2]
        overlap = sum(t in q for t in toks)                 # 담보 토큰 겹침(12개월 vs 6개월 구분)
        return (overlap, r.get("period_bucket") is None)    # 겹침 큰 것, 그다음 정률 우선
    hits.sort(key=_score, reverse=True)
    return hits[0]


def _fmt(r: dict) -> str:
    if not r:
        return "관련 지급규칙을 찾지 못했습니다(→RAG)."
    parts = [f"{r['coverage']}"]
    if r.get("age_band"):       parts.append(f"({r['age_band']})")
    if r.get("period_bucket"):  parts.append(f"[{r['period_bucket']}]")
    unit = f"{r['per_unit']} " if r.get("per_unit") else ""
    parts.append(f"→ 가입금액의 {unit}{r['rate_pct']}%")
    if r.get("limit_days"):         parts.append(f"(한도 {r['limit_days']}일)")
    if r.get("reduction_rate_pct"): parts.append(f"※{r['reduction_period']} {r['reduction_cause']} 시 {r['reduction_rate_pct']}% 감액")
    return " ".join(parts)


def main():
    if len(sys.argv) > 1:                                 # 단건 질의
        r = answer(sys.argv[1])
        print(_fmt(r))
        return

    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    ok = 0
    print(f"{'질의':<38}{'기대':<8}{'답':<8}판정")
    print("-" * 68)
    for g in rows:
        r = answer(g["query"])
        got = r.get("rate_pct") if r else None
        hit = (got == g["expect_rate"])
        ok += hit
        print(f"{g['query'][:36]:<38}{str(g['expect_rate']):<8}{str(got):<8}{'✅' if hit else '❌'}")
    print("-" * 68)
    print(f"정확도 {ok}/{len(rows)}  →  SQL 경로가 '얼마/언제'를 결정론으로 답함")


if __name__ == "__main__":
    main()
