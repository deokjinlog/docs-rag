"""면책기간·감액 추출 — 소비자 "언제부터 (온전히) 받나?". KB 복합약관의 담보별 결정론 사실.

KB 간편건강보험은 기저 지급액이 대부분 '가입금액'(소비자 설정값)이라 지급률은 결정론 불가(→RAG)
지만, **면책기간(가입 후 N일 보장 제외)·감액(가입 후 N년간 M% 지급)은 2열 표에 명시**돼 정밀
추출된다. 소스는 raw md(파이프 표 보존) — 재구성 clean.md는 표 구조를 잃어 여기선 raw를 읽는다.
표 행(담보명|조건)은 자립적이라 다단 뒤섞임에 무해.

precision-first: '가입 후 N일간 보장 제외'만 면책기간으로(재진단암 '진단 후 N년간'=다른 개념 제외).
감액은 '가입 후 N년간 보험금/가입금액 M% 지급' 주 티어 + '(단, K일미만 L%)' 서브 티어.

용법: python3 scripts/extract_waiting.py           # 골든 채점
"""
import os
import re
import glob
import json
import unicodedata

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_waiting.jsonl")

# 면책기간 — '가입 후 N일간 보장 제외'만(진단 후 N년간=재진단 간격, 다른 개념 → 제외)
_WAIT_RE = re.compile(r"가입\s*후?\s*(\d+)\s*일간\s*보장\s*제외")
# 감액 주 티어 — '가입 후 N년간 (보험금|가입금액) M% 지급'
_RED_RE = re.compile(r"가입\s*후?\s*(\d+)\s*년간\s*(?:보험금|가입금액)\s*(\d+)\s*%\s*지급")
# 감액 서브 티어 — '(단, K일미만 L% 지급)'
_SUB_RE = re.compile(r"단,?\s*(\d+)\s*일\s*미만\s*(\d+)\s*%")


def parse_cond(cond: str) -> dict:
    """조건 문자열 → {waiting_days, reduction_period, reduction_rate_pct, sub_*}. 순수(테스트용).

    '가입 후 90일간 보장 제외' → waiting_days=90. '가입 후 1년간 보험금 50% 지급 (단,90일미만
    10%)' → reduction 1년이내·50 + sub 90일·10. 매칭 없으면 None(precision-first).
    """
    r = {"waiting_days": None, "reduction_period": None, "reduction_rate_pct": None,
         "sub_period_days": None, "sub_rate_pct": None}
    w = _WAIT_RE.search(cond)
    if w:
        r["waiting_days"] = int(w.group(1))
    rd = _RED_RE.search(cond)
    if rd:
        r["reduction_period"] = f"{rd.group(1)}년이내"
        r["reduction_rate_pct"] = int(rd.group(2))
        sub = _SUB_RE.search(cond)
        if sub:
            r["sub_period_days"] = int(sub.group(1))
            r["sub_rate_pct"] = int(sub.group(2))
    return r


def _norm_cov(s: str) -> str:
    """담보명 정규화 — 괄호·<br>·공백 제거해 매칭 키로."""
    s = re.sub(r"\([^)]*\)", "", s.replace("<br>", " "))
    return re.sub(r"\s+", "", s).strip()


def _raw_md(doc: str) -> str:
    return open(next(p for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if doc in p),
                encoding="utf-8").read()


_CACHE: dict = {}


def extract_waiting(doc: str) -> dict:
    """문서의 담보별 면책기간·감액 → {정규화담보명: {coverage, waiting_days, reduction_*}}.

    파이프 표 행(담보명 | 조건)에서 셀0=담보명, 뒷셀=면책/감액 조건. 담보 1개가 면책표·감액표
    양쪽에 나오므로 정규화 담보명으로 누적 병합.
    """
    if doc in _CACHE:
        return _CACHE[doc]
    out: dict = {}
    for ln in _raw_md(doc).split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        cov_raw = cells[0]
        key = _norm_cov(cov_raw)
        if len(key) < 3 or "담보명" in cov_raw or "---" in cov_raw:   # 헤더·구분선 배제
            continue
        cond = " ".join(cells[1:])
        p = parse_cond(cond)
        if p["waiting_days"] is None and p["reduction_period"] is None:
            continue
        rec = out.setdefault(key, {"coverage": re.sub(r"\s+", " ", cov_raw.replace("<br>", " ")).strip(),
                                   "waiting_days": None, "reduction_period": None,
                                   "reduction_rate_pct": None, "sub_period_days": None, "sub_rate_pct": None})
        if p["waiting_days"] is not None and rec["waiting_days"] is None:
            rec["waiting_days"] = p["waiting_days"]
        if p["reduction_period"] is not None and rec["reduction_period"] is None:
            rec.update({k: p[k] for k in ("reduction_period", "reduction_rate_pct",
                                          "sub_period_days", "sub_rate_pct")})
    _CACHE[doc] = out
    return out


def predict(doc: str, coverage: str, field: str):
    """골든 담보명(부분일치)으로 레코드를 찾아 field 값 반환."""
    key = _norm_cov(coverage)
    recs = extract_waiting(doc)
    for k, r in recs.items():
        if key in k or k in key:
            return r.get(field)
    return None


# ── 골든 채점 ──────────────────────────────────────────────────────
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
    print(f"{'문서':<12}{'담보':<20}{'필드':<20}{'정답':<8}{'추출':<8}판정")
    print("-" * 76)
    for r in rows:
        pred = predict(r["doc"], r["coverage"], r["field"])
        cat, v = _judge(r["expected"], pred)
        C[cat] += 1
        print(f"{r['doc'][:10]:<12}{r['coverage'][:18]:<20}{r['field']:<20}"
              f"{str(r['expected']):<8}{str(pred):<8}{v}")
    print("-" * 76)
    rec = C["TP"] / (C["TP"] + C["FN"]) if (C["TP"] + C["FN"]) else 1.0
    prec = C["TP"] / (C["TP"] + C["FP"]) if (C["TP"] + C["FP"]) else 1.0
    print(f"recall={rec:.2f} precision={prec:.2f} "
          f"(TP{C['TP']} FN{C['FN']} FP{C['FP']} TN{C['TN']})  FP={C['FP']}")


if __name__ == "__main__":
    main()
