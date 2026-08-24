"""SQL 경로 — payout_rule 결정론 질의 (3경로 라우터 B5의 SQL 경로).

"얼마 받아요/언제부터 온전히?" 질의를 payout_rule SELECT로 **결정론** 답변한다. RAG처럼
확률적 해석이 아니라 (담보·원인·연령·경과기간) 의도를 규칙으로 뽑아 값을 집어온다.

**설계 — 순수 로직 + 주입된 rows**: 이 모듈은 DB에 의존하지 않는다(stdlib re만). rows는
호출자가 `PayoutRepository.get_rules()`(실 DB) 또는 골든 픽스처로 주입 → 서빙·테스트 공용
단일 소스. `scripts/query_payout.py`(골든 러너)도 이 모듈을 import해 같은 로직을 검증한다.

라우터 배선(B5): 라우터가 `select_payout(repo.get_rules(...), query)`를 호출, hit면 결정론
답변, miss면 RAG로 폴백(`format_payout`의 "→RAG" 신호).
"""

from __future__ import annotations

import re

# 담보 키워드 — 질의어 ↔ payout_rule.coverage 매칭용
_COVERAGE_KEYWORDS = ["중환자실", "레진", "아말감", "인레이", "제자리암", "암진단자금", "소득보장"]

# /answer 자동 라우팅 게이트 — "얼마/지급률"처럼 결정론 지급값을 묻는 질의만 SQL로.
# precision-first: 담보만 언급하고 '언제 지급(지급사유)·정의·방법'을 묻는 해석 질의는 RAG 소관
# (select_payout은 담보만 있으면 매칭되므로, /answer에선 이 게이트가 먼저 통과해야 SQL 호출).
# '며칠·한도'는 보장한도(별표)와 겹쳐 오라우팅 위험이라 제외 — 명확한 지급액 신호만 채택.
_AMOUNT_INTENT_RE = re.compile(
    r"얼마|금액|지급률|지급액|몇\s*(?:퍼센트|%|프로)|퍼센트|감액|하루\s*얼마|매월\s*얼마"
)


def is_payout_amount_query(query: str) -> bool:
    """이 질의가 '얼마/지급률' 같은 결정론 지급값을 묻나 — /answer의 SQL 라우팅 게이트.

    True여도 담보·규칙이 안 맞으면 select_payout이 None을 내 RAG로 폴백(2중 안전). 이 게이트의
    역할은 '지급사유·정의·절차'처럼 담보만 겹치는 해석 질의가 SQL로 새는 걸 막는 것.
    """
    return bool(_AMOUNT_INTENT_RE.search(query))

# 원인·연령·경과기간은 하드 필터(담보가 그 축을 쓸 때만)
_HARD_FILTER_KEYS = ("cause", "age_band", "period_bucket")


def extract_payout_intent(query: str) -> dict:
    """질의에서 (담보·원인·연령·경과기간) 의도 추출 — 규칙 기반, LLM 없음."""
    intent: dict = {}
    for kw in _COVERAGE_KEYWORDS:
        if kw in query:
            intent["coverage"] = kw
            break
    # '재해 아닌'=질병. 홑 '외'는 외상/외과 오매칭이라 뺌.
    if re.search(r"재해\s*(?:가\s*)?(?:아닌|이외|제외|아니)", query):
        intent["cause"] = "질병"
    elif re.search(r"질병|병으로|아파", query):
        intent["cause"] = "질병"
    elif re.search(r"재해|상해|다쳐|사고", query):
        intent["cause"] = "상해"
    if re.search(r"15세\s*이상|성인", query):
        intent["age_band"] = "15세이상"
    elif re.search(r"15세\s*미만|어린|아동", query):
        intent["age_band"] = "15세미만"
    if re.search(r"90일\s*(이내|안|이하|전)", query):
        intent["period_bucket"] = "90일이하"
    elif re.search(r"1년\s*(지나|이상|후|넘)", query):
        intent["period_bucket"] = "1년이상"
    elif re.search(r"90일.*1년|1년.*90일|반년|몇\s*달", query):
        intent["period_bucket"] = "90일초과1년미만"
    return intent


def select_payout(rows: list[dict], query: str) -> dict | None:
    """질의 → payout_rule 필터 → 결정론 결과 1건(없으면 None → RAG 폴백).

    rows: payout_rule row dict 리스트(coverage/cause/age_band/period_bucket/rate_pct/
          per_unit/limit_days/reduction_* 키). 실 DB는 `PayoutRepository.get_rules()`.
    """
    intent = extract_payout_intent(query)
    # precision-first: 담보를 못 짚으면 결정론 답 불가 → None(RAG 폴백). 라우터가 SQL로
    # 보냈어도 담보 미검출이면 억지 매칭(전 rows 후보화)으로 엉뚱한 지급률을 뱉지 않는다.
    if "coverage" not in intent:
        return None
    # rate_pct=NULL 행은 기저 지급률이 없는 KB 감액전용 행(면책기간·감액만) — "얼마?"의 결정론
    # 답이 될 수 없으므로(기저는 가입금액 상대) payout 후보에서 제외. KB 감액은 waiting 경로 소관.
    cands = [r for r in rows
             if intent["coverage"] in (r.get("coverage") or "") and r.get("rate_pct") is not None]
    hits = []
    for r in cands:
        skip = False
        for k in _HARD_FILTER_KEYS:
            if k not in intent:
                continue
            # 이 담보가 안 쓰는 축이면 필터 스킵(예: 중환자실=cause 없음)
            if all((c.get(k) or None) is None for c in cands):
                continue
            if (r.get(k) or None) != intent[k]:
                skip = True
                break
        if not skip:
            hits.append(r)
    if not hits:
        return None

    def _score(r: dict):
        toks = [t for t in re.split(r"[\s()/]+", r.get("coverage") or "") if len(t) >= 2]
        overlap = sum(t in query for t in toks)          # 담보 토큰 겹침(12개월 vs 6개월 구분)
        return (overlap, r.get("period_bucket") is None)  # 겹침 큰 것, 그다음 정률 우선

    hits.sort(key=_score, reverse=True)
    return hits[0]


def format_payout(r: dict | None) -> str:
    """결정론 결과를 소비자 답변 한 줄로. None이면 RAG 폴백 신호."""
    if not r:
        return "관련 지급규칙을 찾지 못했습니다(→RAG)."
    parts = [f"{r['coverage']}"]
    if r.get("age_band"):
        parts.append(f"({r['age_band']})")
    if r.get("period_bucket"):
        parts.append(f"[{r['period_bucket']}]")
    unit = f"{r['per_unit']} " if r.get("per_unit") else ""
    parts.append(f"→ 가입금액의 {unit}{r['rate_pct']}%")
    if r.get("limit_days"):
        parts.append(f"(한도 {r['limit_days']}일)")
    if r.get("reduction_rate_pct"):
        # 없는 조건은 생략(None 리터럴 방지)
        cond = " ".join(x for x in (r.get("reduction_period"), r.get("reduction_cause")) if x)
        parts.append(f"※{cond + ' 시' if cond else ''} {r['reduction_rate_pct']}% 감액".strip())
    return " ".join(parts)


# 표준 면책 사유 → 원문 키워드(표기변이 흡수). 표준약관 공통 + 상품별. 오프라인 골든
# extract_exclusion_reasons.py(12/12)와 서빙이 공유하는 **단일 소스**(그쪽이 이걸 import).
EXCLUSION_TITLE_KW = ("지급하지 않", "지급하지아니", "보상하지 않", "보장하지 않")
EXCLUSION_REASON_KW = [
    ("고의", ["고의로"]),
    ("임신출산", ["임신", "출산", "산후"]),
    ("전쟁내란", ["전쟁", "무력행사", "혁명", "내란", "사변", "폭동"]),
    ("위험활동", ["전문등반", "글라이더", "스카이다이빙", "행글라이딩", "전문적인 등산", "모터보트", "자동차경기"]),
    ("무면허운전", ["무면허"]),
    ("음주운전", ["음주운전", "주취운전", "주취 상태"]),
    ("직업위험", ["직업, 직무", "직무 또는 동호회"]),
]


def extract_exclusion_tags(body: str | None) -> list[str]:
    """면책 조 본문에서 표준 면책 사유 태그 추출(고의·전쟁내란 등). 없으면 []."""
    if not body:
        return []
    return sorted({tag for tag, kws in EXCLUSION_REASON_KW if any(k in body for k in kws)})


def format_exclusion_note(exclusions: list[dict]) -> str:
    """면책(지급 제외) 강제첨부 — "얼마?" 답에 항상 붙인다(지급률만 답하고 면책 빠뜨리면
    소비자 손해, domain-model.md). exclusions=general 면책 조 [{jo, title, body?}]. 없으면 "".

    body가 있으면 실제 사유 태그(고의·전쟁내란 등)를, 없으면 조 참조만. 확정 안내가 아니라
    "확인 필요" 톤 — 부모 보통약관 공통면책이 corpus에 없을 수 있어 안전하다 단정하지 않는다.
    """
    if not exclusions:
        return ""
    items = []
    for e in exclusions:
        if not e.get("jo"):
            continue
        tags = extract_exclusion_tags(e.get("body"))
        ref = f"제{e['jo']}조"
        items.append(f"{'·'.join(tags)} 등({ref})" if tags else f"{ref}({e['title']})" if e.get("title") else ref)
    return f"※ 지급 제외(면책): {' / '.join(items)} 확인 필요" if items else ""


def format_payout_complete(rule: dict | None, exclusions: list[dict] | None = None) -> str:
    """완결 답변 = 지급값 + 면책 강제첨부. rule None이면 RAG 폴백 신호 그대로."""
    base = format_payout(rule)
    if rule is None:
        return base
    note = format_exclusion_note(exclusions or [])
    return f"{base}  {note}" if note else base
