"""SQL 경로 — 면책 상세. 소비자 "뭐가 안 돼요?(면책)".

상품의 면책 조(coverage_exclusion_map kind='general')에서 실제 사유 태그(고의·전쟁내란 등)를
뽑아 나열. 태그 로직은 payout_sql.extract_exclusion_tags(오프라인 골든 12/12와 단일 소스).
payout의 강제첨부(답에 항상 붙임)와 달리 이건 "면책만" 묻는 단독 질의의 결정론 답.

상품 해소는 담보 키워드(terms_sql.coverage_hint) → ProductRepository.get_terms. 못 짚으면 RAG.
"""

from __future__ import annotations

import re

from .payout_sql import extract_exclusion_tags

# 면책 질의 게이트 — "면책/지급 안 되는 사유/보장 안 되는/제외되는"
_EXCLUSION_INTENT_RE = re.compile(
    r"면책|지급.{0,3}(?:안|하지\s*않|되지\s*않|제외)|보장.{0,3}(?:안|되지\s*않|제외)"
    r"|보상.{0,3}(?:안|않)|안\s*(?:되는|받는).{0,4}(?:경우|사유|때)|제외.{0,3}(?:사유|되는|경우)"
)


def is_exclusion_query(query: str) -> bool:
    """"뭐가 면책?/지급 안 되는 경우?" 류 면책 상세 질의인가 — /answer의 exclusion 라우팅 게이트."""
    return bool(_EXCLUSION_INTENT_RE.search(query))


def common_exclusion_note(resolution_note: str | None) -> str:
    """특약 공통면책 준용 완결성 — 특약은 고유 면책(고의)만 자체 보유하고 공통면책(전쟁·임신·
    위험활동 등)은 주계약 준용 소관. 주계약 미확보면 **없는 걸 안전하다 하지 않고** 명시한다
    (domain-model.md 면책 준용, precision-first). resolution_note에 '준용'이 있으면 특약 신호.
    """
    if resolution_note and "준용" in resolution_note:
        m = re.search(r"제\s*\d+\s*조", resolution_note)
        ref = m.group(0).replace(" ", "") if m else "주계약"
        return f"※ 공통면책(전쟁·임신·위험활동 등)은 {ref} 준용 소관 — 주계약 미확보로 확인 필요"
    return ""


def format_exclusions(exclusions: list[dict], resolution_note: str | None = None) -> str:
    """상품 면책 조 [{jo, title, body}] → 실제 사유 나열 + **준용 완결성**. 없으면 RAG 폴백.

    body에서 표준 태그(고의·전쟁내란 등) 합집합 + 조 참조. 특약이면(resolution_note에 준용)
    공통면책 준용 미확보를 강제첨부해 "완결"(빠짐 없음)·"정직"(없는 걸 안전하다 안 함).
    """
    common = common_exclusion_note(resolution_note)
    if not exclusions:
        # 특약이라 자체 면책 조가 없어도 준용 공통면책은 안내(완전 침묵 방지)
        return common if common else "면책(지급 제외) 조를 찾지 못했습니다(→RAG)."
    tags: list[str] = []
    for e in exclusions:
        for t in extract_exclusion_tags(e.get("body")):
            if t not in tags:
                tags.append(t)
    refs = " · ".join(f"제{e['jo']}조" for e in exclusions if e.get("jo"))
    if tags:
        base = f"지급 제외(면책) 사유: {'·'.join(tags)} 등 ({refs}) — 상세는 해당 조 확인 필요"
    elif refs:
        base = f"지급 제외(면책): {refs} 참조 — 확인 필요"
    else:
        return common if common else "면책 조를 찾지 못했습니다(→RAG)."
    return f"{base}  {common}" if common else base
