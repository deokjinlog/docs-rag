"""지급기준표(별표1) 행 분해 추출기 — 소비자 QA "얼마/언제"의 결정론 값.

별표1 '보험금 지급기준표'를 행 단위로 쪼개 payout_rule을 뽑는다:
  담보(급부명) × 원인/경과기간 → 지급률·단위·한도·감액.
"얼마 받아요/언제부터 온전히?"의 답이 이 표에 조건부로 들어있어 스칼라 컬럼으론 부족 → 행 분해.

precision-first: 못 뽑으면 NULL(→RAG). 별표1 '영역'에 앵커링해 목차성 요약행 과탐을 배제한다.
룰베 코어 — 표 구조가 불규칙한 문서(New치아 기간구간 매트릭스 등)는 LLM 폴백 지점(TODO).

용법: python3 scripts/extract_payout.py           # 골든 채점(루프 닫기)
"""
import re
import json
import pathlib
import unicodedata

_HERE = pathlib.Path(__file__).parent
GOLDEN = _HERE.parent / "data" / "eval" / "golden_payout.jsonl"

def _threecol_rows(md: str):
    """정확히 3열(급부명|지급사유|지급금액)이고 지급금액 칸에 '가입금액'이 있는 표 행.

    heading 앵커 대신 '내용 기반' — 문서마다 '지급기준표' 제목 위치가 달라 앵커(마지막 heading)가
    표를 놓치던 문제(라이나_소득보장 0행) 회피. New치아·다이렉트의 5열 경과기간 매트릭스는
    프로파일 B가 담당하므로 '정확히 3열' 한정이 두 프로파일을 자연 분리한다."""
    for ln in md.split("\n"):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) != 3:                              # 3열만 (매트릭스 5열은 B 담당)
            continue
        c0, c2 = cells[0], cells[2]
        if not c0 or "---" in c0 or c0.replace(" ", "").isdigit():   # 담보명은 '12개월…'처럼 숫자로 시작 가능 → 전체가 숫자일 때만 스킵
            continue
        if any(k in c0 for k in ("급부명", "구분", "담보명", "대상")):   # 헤더행
            continue
        if "가입금액" not in c2:                          # 지급금액 칸에 가입금액 = payout 표
            continue
        yield cells


def _clean_cov(s: str) -> str:
    """담보명 정리 — OCR 잔재(<br>) 제거 + 공백 정규화."""
    return re.sub(r"\s+", " ", s.replace("<br>", " ")).strip()


def _parse_row(cells: list) -> dict:
    """지급기준표 한 행 → payout_rule. 결정론(정규식). 못 뽑은 필드는 None."""
    coverage = _clean_cov(cells[0])
    cond = cells[1]                                        # 지급사유(한도 포함)
    amount = cells[2].replace("<br>", " ")                # 지급금액(지급률+감액)
    both = cond + " " + amount
    r = {"coverage": coverage}

    m = re.search(r'가입금액의?\s*([0-9]+(?:\.[0-9]+)?)\s*%', amount)   # 기본 지급률
    r["rate_pct"] = (int(m.group(1)) if m and "." not in m.group(1)
                     else float(m.group(1)) if m else None)

    pu = re.search(r'(1일당|1회당|매월|매년|일당|회당)', amount)        # 지급 단위(일/회/월)
    r["per_unit"] = pu.group(1) if pu else None

    lm = re.search(r'([0-9]+)\s*일\s*한도', both)                       # 한도(일수)
    r["limit_days"] = int(lm.group(1)) if lm else None

    red = re.search(r'상기\s*금액의?\s*([0-9]+)\s*%', amount)           # 감액 지급률
    r["reduction_rate_pct"] = int(red.group(1)) if red else None

    per = re.search(r'([0-9]+)\s*년이?\s*지난.*전일\s*이전', amount)     # "1년이 지난 전일 이전" = 1년이내
    r["reduction_period"] = f"{per.group(1)}년이내" if per else None

    # "재해 이외" / "재해는 제외" / "재해 제외" 모두 = 재해외 원인만 감액 대상
    r["reduction_cause"] = "재해외" if re.search(r'재해\s*(?:이\s*외|는?\s*제외|를\s*제외)', amount) else None
    return r


# ── 프로파일 B: 기간구간 매트릭스 (New치아식 담보×원인×경과기간 → 지급률) ──
RE_CAUSE = re.compile(r'(상해|재해|질병)\s*[을를]?\s*원인')     # ◦ 상해를 원인으로 / 질병을 원인으로
RE_BUCKET_HDR = re.compile(r'90\s*일\s*(이하|초과)')            # 경과기간 매트릭스 헤더 감지


def _bucket(h: str):
    """경과기간 헤더 셀 → 정규화 버킷 라벨."""
    h = h.replace(" ", "")
    if "이하" in h:               return "90일이하"
    if "초과" in h and "미만" in h: return "90일초과1년미만"
    if "이상" in h:               return "1년이상"
    return None


def _matrix_rules(md: str) -> list:
    """경과기간 매트릭스 표 → payout_rule 행. 원인은 직전 '◦ ~를 원인으로' 문맥에서."""
    lines = md.split("\n")
    rules, cause, i = [], None, 0
    while i < len(lines):
        ln = lines[i]
        cm = RE_CAUSE.search(ln)
        if cm:
            cause = "상해" if cm.group(1) in ("상해", "재해") else "질병"
        if ln.strip().startswith("|") and RE_BUCKET_HDR.search(ln):     # 버킷 헤더 행
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            buckets = {idx: _bucket(c) for idx, c in enumerate(cells) if _bucket(c)}
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("|"):   # 이어지는 데이터 행
                dc = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                cov = _clean_cov(dc[0])
                if cov and "---" not in cov:
                    filled = []
                    for idx, bk in buckets.items():
                        if idx < len(dc):
                            m = re.search(r'가입금액의?\s*([0-9]+)\s*%', dc[idx])
                            if m:
                                filled.append((bk, int(m.group(1))))
                    if len(filled) == 1:                                  # 1칸만 = 정률(상해)
                        rules.append({"coverage": cov, "cause": cause,
                                      "period_bucket": None, "rate_pct": filled[0][1]})
                    else:                                                 # 여러 칸 = 경과기간별(질병)
                        for bk, rate in filled:
                            rules.append({"coverage": cov, "cause": cause,
                                          "period_bucket": bk, "rate_pct": rate})
                j += 1
            i = j
            continue
        i += 1
    return rules


_CACHE = {}


def extract_payout(doc: str) -> list:
    """문서의 지급기준 → payout_rule 목록. 프로파일 A(별표1 3열) + B(기간구간 매트릭스)."""
    if doc not in _CACHE:
        import glob
        path = next(p for p in glob.glob("data/output/raw/*.md") if doc in p)
        md = open(path, encoding="utf-8").read()
        rules = [_parse_row(c) for c in _threecol_rows(md)]            # 프로파일 A (3열 지급기준표)
        rules += _matrix_rules(md)                                     # 프로파일 B (경과기간 매트릭스)
        _CACHE[doc] = rules
    return _CACHE[doc]


_FILTER_KEYS = ("coverage", "cause", "period_bucket")


def predict(row: dict):
    """골든 행이 지정한 키(coverage/cause/period_bucket)로 규칙을 찾아 field 값 반환."""
    for r in extract_payout(row["doc"]):
        ok = True
        for k in _FILTER_KEYS:
            if k not in row:
                continue                                              # 골든이 안 지정 → 필터 안 함
            gv, rv = row[k], r.get(k)
            if k == "coverage":
                if not (gv in (rv or "") or (rv or "") in gv):
                    ok = False; break
            elif rv != gv:                                            # cause·period_bucket 정확 일치(null 포함)
                ok = False; break
        if ok:
            return r.get(row["field"])
    return None


# ── 골든 채점 (설계→구현→골든셋 루프 닫기) ────────────────────────────────
def _norm(s):
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[,.·“”\"'()\[\]%]", "", s)
    return s.lower()


def _judge(gold, pred):
    g, p = _norm(gold), _norm(pred)
    if g is not None and p == g:     return "TP", "✅ TP"
    if g is not None and p is None:  return "FN", "❌ FN(놓침)"
    if g is not None and p != g:     return "FP", f"❌ FP(틀림→{pred})"
    if g is None and p is None:      return "TN", "✅ TN(맞게비움)"
    return "FP", "❌ FP(헛짚음)"


def main():
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    from collections import Counter
    C = Counter()
    print(f"{'문서':<10}{'담보':<8}{'원인/기간':<18}{'필드':<12}{'정답':<6}{'추출':<6}판정")
    print("-" * 78)
    for r in rows:
        pred = predict(r)
        cat, v = _judge(r["expected"], pred)
        C[cat] += 1
        key = f"{r.get('cause') or ''}/{r.get('period_bucket') or ''}"
        print(f"{r['doc'][:8]:<10}{r['coverage'][:6]:<8}{key:<18}"
              f"{r['field']:<12}{str(r['expected']):<6}{str(pred):<6}{v}")
    print("-" * 68)
    rec = C["TP"] / (C["TP"] + C["FN"]) if (C["TP"] + C["FN"]) else 1.0
    prec = C["TP"] / (C["TP"] + C["FP"]) if (C["TP"] + C["FP"]) else 1.0
    print(f"recall={rec:.2f}  precision={prec:.2f}  "
          f"(TP{C['TP']} FN{C['FN']} FP{C['FP']} TN{C['TN']})  **확신에 찬 오답 FP={C['FP']}**")


if __name__ == "__main__":
    main()
