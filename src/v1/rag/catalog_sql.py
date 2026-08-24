"""SQL 경로 — 담보 catalog 멤버십 결정론 질의. 소비자 "이 상품에 X 담보 있어? / 뭐 보장해?".

payout("얼마")·coverage("이 코드 보장돼?"=별표3 ICD)와 다른 축 — **담보 존재 여부**(가장 흔한
소비자 보장 질문). KB 복합약관은 특약 1개=담보 1개라 특약 목록이 곧 catalog(`list_catalog`).

**설계 — 순수 로직 + 주입된 catalog**: DB 무의존(stdlib re만). catalog(특약 이름 리스트)는 호출자가
`CoverageRepository.list_catalog(base)`로 주입 → 서빙·테스트 공용. precision-first: 질의의 담보가
catalog에 있으면 "있음"만 단정하고, 못 찾으면 **부재를 단정하지 않고** matched=false→RAG(담보가
동의어·다른 표기로 있을 수 있어 "없다"는 확신에 찬 오답 위험).
"""

from __future__ import annotations

import re

# 멤버십 의도 게이트 — "담보 있어/보장 포함/뭐 보장해". '얼마'(payout)·ICD 코드(coverage)와 배타.
_CATALOG_INTENT_RE = re.compile(
    r"담보[^\n]{0,8}(있|없|포함|가입|보장|되나|돼)"
    r"|보장[^\n]{0,4}(하나|되나|돼|하는지|합니까|되는지|되어|해\s*주)"
    r"|보장\s*(항목|내용|담보|되는)"
    r"|무슨\s*담보|어떤\s*담보|담보\s*(목록|종류)|뭐\s*보장|무엇을?\s*보장"
)

# 담보명이 아닌 일반어(단독으론 멤버십 매칭 금지 — '상해' 하나로 수십 담보 오적중 방지)
_GENERIC = frozenset({"상해", "질병", "보장", "담보", "진단", "수술", "입원", "치료", "재해", "사망"})


def is_catalog_query(query: str) -> bool:
    """이 질의가 '이 담보 있어?/뭐 보장해?' 같은 담보 존재를 묻나 — /answer catalog 라우팅 게이트.

    True여도 브랜드 상품 미해소·담보 미적중이면 matched=false→RAG(2중 안전, precision-first).
    """
    return bool(_CATALOG_INTENT_RE.search(query))


def normalize_coverage(name: str) -> str:
    """특약 이름 → 담보명. 'N.'/'N-N.' 번호접두·괄호·한 줄 병합 갱신계약 변형 제거."""
    name = re.sub(r"^\s*\d+(?:-\d+)?\.\s*", "", name)      # 선두 'N. '/'N-N. '
    name = re.split(r"\s+\d+-\d+\.\s*|【", name)[0]         # 병합된 2번째 변형/【갱신계약】 컷
    name = re.sub(r"\([^)]*\)", "", name)                   # 괄호(간편가입·급수 등)
    return re.sub(r"\s+", " ", name).strip()


def find_covered(catalog: list[str], query: str) -> list[str]:
    """catalog(특약 이름들) 중 질의에 등장하는 담보명 목록(정규화·중복제거).

    매칭 = 정규화 담보명(≥4자, 일반어 아님)이 질의의 부분문자열. 소비자가 담보를 이름으로 지목한
    경우만 결정론 확정(precision-first — '파킨슨병진단비 있어?'는 잡고 '파킨슨병 보장?'은 RAG 소관).
    """
    q = re.sub(r"\s+", "", query)
    out: list[str] = []
    for raw in catalog:
        cov = normalize_coverage(raw)
        key = re.sub(r"\s+", "", cov)
        if len(key) < 4 or cov in _GENERIC:
            continue
        if key in q and cov not in out:
            out.append(cov)
    return out


def format_catalog(covered: list[str], base_name: str | None = None) -> str:
    """멤버십 결정론 답. 적중 없으면 RAG 폴백 신호(부재 단정 안 함)."""
    if not covered:
        return "해당 담보를 상품 담보목록에서 확정하지 못했습니다(→RAG)."
    where = f"{base_name} " if base_name else ""
    return f"네, {where}보장 담보에 있습니다: {' · '.join(covered)} (근거: 해당 특약)"
