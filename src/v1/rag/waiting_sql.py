"""SQL 경로 — 면책기간·감액 결정론 질의. 소비자 "언제부터 (온전히) 받나? / 면책기간·감액?".

KB 복합약관은 담보(특약)별로 **면책기간(가입 후 N일 보장 제외)·감액(가입 후 1년간 M% 지급)**이
명시된다(payout 기저율은 가입금액이라 결정론 불가지만 이 둘은 정밀). 특약 1개=담보 1개라 질의의
담보명으로 특약을 짚어 그 특약의 waiting_period_days + 감액(payout_rule source='kb_table')을 답한다.

**설계 — 순수 로직 + 주입된 rows**: DB 무의존. rows(특약 이름+면책+감액)는 `CoverageRepository.
get_waiting_facts(base)`로 주입. precision-first: 담보 미지목이면 None→RAG, 면책·감액 둘 다 없으면
matched=false→RAG(억지 답 안 함).
"""

from __future__ import annotations

import re

from .catalog_sql import normalize_coverage

# 면책기간·감액 의도 게이트 — "면책기간·언제부터·보장 제외·감액·온전히". 기저 '얼마'(payout)와 구분.
_WAIT_INTENT_RE = re.compile(
    r"면책\s*기간|언제부터|보장\s*(?:개시|시작)|보장\s*제외|감액|온전히|"
    r"가입\s*후\s*(?:얼마|언제|며칠|몇\s*일)|기다려야|지나야"
)


def is_waiting_query(query: str) -> bool:
    """면책기간·감액을 묻나 — /answer waiting SQL 라우팅 게이트."""
    return bool(_WAIT_INTENT_RE.search(query))


_ROMAN_SUFFIX = re.compile(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+$")


def pick_subcontract(rows: list[dict], query: str) -> dict | None:
    """rows(특약 이름+면책+감액) 중 질의에 등장하는 담보의 행. 담보 미지목이면 None(→RAG).

    rows[i]: {product_name, waiting_period_days, reduction_period, reduction_rate_pct}.
    ①정확 매칭(특약 전체 이름이 질의에 등장) 최우선·가장 구체적(긴 것). ②없으면 로마자 접미 제거
    매칭('질병사망'→'질병사망Ⅲ') — 단 그 base 담보가 **유일**할 때만(암수술비Ⅰ vs Ⅱ 모호는 RAG,
    precision-first). 매칭돼도 면책·감액 둘 다 없으면 호출자가 matched=false 처리.
    """
    q = re.sub(r"\s+", "", query)
    exact = []
    for r in rows:
        cov = normalize_coverage(r.get("product_name") or "")
        key = re.sub(r"\s+", "", cov)
        if len(key) >= 4 and key in q:
            exact.append((len(key), r, cov))
    if exact:
        exact.sort(key=lambda x: -x[0])
        return {**exact[0][1], "_coverage": exact[0][2]}

    # 로마자 접미 제거 매칭 — base가 질의에 있고 그 base 담보가 유일한 특약일 때만
    base_hits: dict = {}
    for r in rows:
        cov = normalize_coverage(r.get("product_name") or "")
        base = _ROMAN_SUFFIX.sub("", re.sub(r"\s+", "", cov))
        if len(base) >= 4 and base in q:
            base_hits.setdefault(base, []).append((r, cov))
    uniq = [lst[0] for lst in base_hits.values() if len(lst) == 1]
    if len(uniq) == 1:
        r, cov = uniq[0]
        return {**r, "_coverage": cov}
    return None


def format_waiting(row: dict | None) -> str:
    """면책기간·감액 결정론 답. row 없거나 면책·감액 둘 다 없으면 RAG 폴백 신호."""
    if not row:
        return "면책기간·감액 정보를 찾지 못했습니다(→RAG)."
    w = row.get("waiting_period_days")
    rp, rr = row.get("reduction_period"), row.get("reduction_rate_pct")
    if w is None and rr is None:
        return "면책기간·감액 정보를 찾지 못했습니다(→RAG)."
    parts = [f"{row.get('_coverage') or row.get('product_name') or ''}".strip()]
    if w is not None:
        parts.append(f"면책기간 {w}일(가입 후 {w}일간 보장 제외)")
    if rr is not None:
        parts.append(f"가입 후 {rp or ''} {rr}% 감액".replace("  ", " ").strip())
    return " · ".join(p for p in parts if p)
