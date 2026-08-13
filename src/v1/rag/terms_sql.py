"""SQL 경로 — 계약조건(청약철회·갱신) 결정론 질의. 소비자 "언제까지?".

payout_sql과 같은 패턴(순수 로직 + 주입된 product row). **핵심 = 준용 NULL 철학**: 특약은
청약철회가 NULL이 정답(보통약관 준용 소관) — 억지 값 대신 "주계약 준용 소관, 확인 필요"로
답한다(precision-first, 확신에 찬 오답 0). 데이터는 `ProductRepository.get_terms`(product
테이블 cooling_off_days·is_renewable·resolution_note, `load_terms.py --load`로 적재).
"""

from __future__ import annotations

import re

# 계약조건 질의 게이트 — "청약철회·갱신·만기·언제까지"
_TERMS_INTENT_RE = re.compile(r"청약\s*철회|철회|갱신|만기|자동\s*갱신|언제까지|며칠\s*이내")

# product 해소용 담보 키워드 (payout_sql과 공유 개념 — 질의어 → 상품)
_COVERAGE_KEYWORDS = ["중환자실", "레진", "치아", "충치", "제자리암", "암진단자금", "소득보장", "입원"]


def is_terms_query(query: str) -> bool:
    """청약철회·갱신 등 계약조건을 묻나 — /answer의 terms SQL 라우팅 게이트."""
    return bool(_TERMS_INTENT_RE.search(query))


def coverage_hint(query: str) -> str | None:
    """질의에서 상품 해소용 담보 키워드 추출(없으면 None → 상품 특정 불가)."""
    for kw in _COVERAGE_KEYWORDS:
        if kw in query:
            return kw
    return None


def _resolution_ref(note: str | None) -> str:
    """resolution_note에서 준용 조(제N조)만 뽑아 간결화. 없으면 '주계약'."""
    m = re.search(r"제\s*\d+\s*조", note or "")
    return m.group(0).replace(" ", "") if m else "주계약"


def format_terms(product: dict | None) -> str:
    """청약철회·갱신 결정론 답 (준용 NULL 처리). None이면 RAG 폴백 신호."""
    if not product:
        return "관련 계약조건을 찾지 못했습니다(→RAG)."
    parts = []
    renew = product.get("is_renewable")
    cycle = product.get("renewal_cycle_years")
    if renew is True:
        parts.append(f"갱신형({cycle}년 주기)" if cycle else "갱신형")
    elif renew is False:
        parts.append("비갱신")
    term = product.get("term_years")
    if term:
        parts.append(f"{term}년 만기")
    cool = product.get("cooling_off_days")
    if cool:
        parts.append(f"청약철회 {cool}일 이내")
    else:
        # 특약 준용 NULL — 억지 답 대신 준용 소관 명시(precision-first)
        ref = _resolution_ref(product.get("resolution_note"))
        parts.append(f"청약철회: {ref} 준용 소관 — 주계약 미확보로 확인 필요")
    return " · ".join(parts) if parts else "관련 계약조건을 찾지 못했습니다(→RAG)."
