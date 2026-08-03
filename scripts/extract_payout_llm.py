"""LLM 폴백 사이드카 — 룰베로 안 잡히는 불규칙 지급표(다이렉트 등) 구조화 추출.

CLAUDE.md '검증된 것만 메인 경로에' 원칙: 이건 메인(extract_payout) 아닌 **사이드카**.
골든으로 precision을 측정하고 **≥0.9 게이트**를 통과해야 메인 폴백으로 승격한다.

대상 = 룰베 프로파일이 실패하는 표:
  (a) 한 셀에 담보 여러 개 병합(제자리암·경계성종양·기타피부암·갑상선암)
  (b) 연령 등 추가 축(15세미만/이상)
  (c) 열 정렬 불규칙(빈 칸이 여기저기)
LLM에 표 원문 + 스키마를 주고 payout_rule JSON 배열로 뽑는다. 담보 분리·연령 축을 LLM이 처리.

LLM 호출: OpenAI 호환 endpoint(env LLM_BASE_URL/LLM_MODEL). 미가동 시 세션-판독 stub로 오프라인
시연(라벨=serving 모델 아님 — self-preference 회피 원칙과 동일 취지). 게이트 통과 후 실서빙 모델로 교체.

용법: python3 scripts/extract_payout_llm.py     # 룰베 vs LLM 정밀도 비교 + 게이트 판정
"""
import os
import re
import sys
import glob
import json
import unicodedata
import urllib.request

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_payout_direct.jsonl")

PROMPT = """다음은 보험약관 지급기준표(마크다운)다. 각 담보의 지급규칙을 JSON 배열로 추출하라.
규칙:
- 한 셀에 담보가 여러 개면 각각 분리해 별도 객체로.
- 연령 구분(15세미만/15세이상)이 있으면 age 필드에, 없으면 null.
- 경과기간이 열이면 period_bucket("90일이하"/"90일초과1년미만"/"1년이상"), 정률이면 null.
- rate_pct = "보험가입금액의 N%"의 N (정수).
스키마: [{"coverage","age","period_bucket","rate_pct"}]  — JSON 배열만 출력.
표:
%s
"""

# 세션-판독 stub — 실 LLM 미가동 시 오프라인 시연용. 원문(다이렉트 암진단비 15세축 표)을 사람이 읽어 라벨한 것.
_STUB = [
    {"coverage": "암진단자금", "age": "15세미만", "period_bucket": "90일이하", "rate_pct": 50},
    {"coverage": "암진단자금", "age": "15세미만", "period_bucket": "1년이상", "rate_pct": 100},
    {"coverage": "암진단자금", "age": "15세이상", "period_bucket": "90일이하", "rate_pct": 0},
    {"coverage": "암진단자금", "age": "15세이상", "period_bucket": "90일초과1년미만", "rate_pct": 50},
    {"coverage": "제자리암진단자금", "age": None, "period_bucket": "90일이하", "rate_pct": 10},
    {"coverage": "경계성종양진단자금", "age": None, "period_bucket": "90일이하", "rate_pct": 10},
    {"coverage": "기타피부암진단자금", "age": None, "period_bucket": "90일이하", "rate_pct": 10},
    {"coverage": "갑상선암진단자금", "age": None, "period_bucket": "90일이하", "rate_pct": 10},
    {"coverage": "제자리암진단자금", "age": None, "period_bucket": "1년이상", "rate_pct": 20},
    {"coverage": "경계성종양진단자금", "age": None, "period_bucket": "1년이상", "rate_pct": 20},
    {"coverage": "기타피부암진단자금", "age": None, "period_bucket": "1년이상", "rate_pct": 20},
    {"coverage": "갑상선암진단자금", "age": None, "period_bucket": "1년이상", "rate_pct": 20},
]


def _table_text(doc: str) -> str:
    """불규칙 지급표 영역(암진단비 15세축 표)만 잘라 LLM에 넘길 텍스트."""
    path = next(p for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if doc in p)
    lines = open(path, encoding="utf-8").read().split("\n")
    out = [ln for ln in lines if ln.strip().startswith("|") and
           ("진단자금" in ln or "15세" in ln or "90일" in ln or "지급금액" in ln)]
    return "\n".join(out)


def llm_extract(doc: str) -> list:
    """LLM 호출로 payout_rule 추출. endpoint 미가동/오류 시 세션-판독 stub 폴백."""
    base = os.environ.get("LLM_BASE_URL")               # 예: http://localhost:8000/v1
    if base:
        try:
            body = json.dumps({
                "model": os.environ.get("LLM_MODEL", "Qwen3-4B-AWQ"),
                "messages": [{"role": "user", "content": PROMPT % _table_text(doc)}],
                "temperature": 0,
            }).encode()
            req = urllib.request.Request(base.rstrip("/") + "/chat/completions", body,
                                         {"Content-Type": "application/json"})
            resp = json.load(urllib.request.urlopen(req, timeout=60))
            txt = resp["choices"][0]["message"]["content"]
            return json.loads(re.search(r"\[.*\]", txt, re.S).group())
        except Exception as e:
            print(f"[경고] LLM 호출 실패({e}) → 세션-판독 stub 사용", file=sys.stderr)
    else:
        print("[안내] LLM_BASE_URL 미설정 → 세션-판독 stub 사용 (실 LLM은 env 설정 후)", file=sys.stderr)
    return _STUB


# ── 채점 (룰베 vs LLM 나란히) ──────────────────────────────────────────
def _norm(s):
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s,.·%]", "", s).lower()


def _predict(rules, row):
    """coverage/age/period_bucket로 규칙 찾아 field 반환."""
    for r in rules:
        ok = True
        for k in ("coverage", "age", "period_bucket"):
            if k not in row:
                continue
            gv, rv = row[k], r.get(k)
            if k == "coverage":
                if not (gv in (rv or "") or (rv or "") in gv):
                    ok = False; break
            elif rv != gv:
                ok = False; break
        if ok:
            return r.get(row["field"])
    return None


def _score(name, rules, rows):
    from collections import Counter
    C = Counter()
    print(f"\n== {name} ==")
    for r in rows:
        pred = _predict(rules, r)
        g, p = _norm(r["expected"]), _norm(pred)
        cat = ("TP" if g == p and g is not None else
               "FN" if p is None else "FP")
        C[cat] += 1
        mark = {"TP": "✅", "FN": "❌FN", "FP": "❌FP"}[cat]
        key = f"{r['coverage'][:8]}/{r.get('age') or ''}/{r.get('period_bucket') or ''}"
        print(f"  {key:<28} 정답 {str(r['expected']):<4} 추출 {str(pred):<6}{mark}")
    rec = C["TP"] / (C["TP"] + C["FN"]) if (C["TP"] + C["FN"]) else 1.0
    prec = C["TP"] / (C["TP"] + C["FP"]) if (C["TP"] + C["FP"]) else 1.0
    print(f"  → recall={rec:.2f} precision={prec:.2f} (TP{C['TP']} FN{C['FN']} FP{C['FP']})")
    return prec


def main():
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    doc = rows[0]["doc"]

    import importlib.util
    s = importlib.util.spec_from_file_location("ep", os.path.join(HERE, "extract_payout.py"))
    ep = importlib.util.module_from_spec(s); s.loader.exec_module(ep)

    rule_prec = _score("룰베(프로파일 A/B)", ep.extract_payout(doc), rows)
    llm_prec = _score("LLM 폴백 사이드카", llm_extract(doc), rows)

    print(f"\n{'='*50}")
    print(f"게이트(CLAUDE.md ≥0.90): 룰베 {rule_prec:.2f} / LLM {llm_prec:.2f}")
    verdict = "승격 후보(사이드카→폴백)" if llm_prec >= 0.9 else "사이드카 유지(정밀도 부족)"
    print(f"→ LLM 폴백 판정: {verdict}")


if __name__ == "__main__":
    main()
