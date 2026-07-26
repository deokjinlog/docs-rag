"""쿼리 라우팅 (1단계) — Rule 기반 결정론적 분기.

사용자 질의를 정규식과 if/elif로 분류해 SearchStrategy·QueryType을 결정한다.
LLM 호출 없음 — 요청당 <1ms.

도메인 비종속: 한국어 일반 문서(매뉴얼·규정·법령·사내 문서) 공통 적용.
도메인 특화 어휘(예: 보험 "보장되나")는 `_INTERPRETATION_INSURANCE` 같은 `_<유형>_<도메인>` 블록으로 분리 —
다른 도메인 전환 시 해당 블록만 교체·삭제·추가하면 됨.

아키텍처: 라우팅 3층 중 1단계 Rule 레이어. 2단계(Domain/Tenant Router) /
3단계(Semantic/LLM Router)는 확장 지점이며 현재 단일 도메인·단일 테넌트라 미구현.
"""

import re
from dataclasses import dataclass
from enum import Enum


class SearchStrategy(Enum):
    BM25_HEAVY = "bm25_heavy"     # 정확 토큰 매칭 우세 (구조적 참조)
    DENSE_HEAVY = "dense_heavy"   # 의미 기반 검색 우세 (해석·절차·비교)
    HYBRID = "hybrid"             # 혼합 또는 판단 불가 (균등 배수)


class QueryType(Enum):
    STRUCTURED_LOOKUP = "structured_lookup"  # 조항·섹션·표·그림 등 번호 붙은 항목 조회
    INTERPRETATION = "interpretation"        # 해석·적용 여부·자격/대상 판단
    PROCEDURE = "procedure"                  # 절차·방법·단계
    COMPARISON = "comparison"                # 둘 이상의 항목 비교
    SIMPLE_FACT = "simple_fact"              # 단순 사실·정의·값 조회


@dataclass
class RouteResult:
    strategy: SearchStrategy
    query_type: QueryType
    # prefetch limit 배수. heavy 쪽 검색에서 더 많은 후보를 RRF 풀에 보냄.
    dense_factor: int
    bm25_factor: int


# --- 구조화된 참조 패턴 (STRUCTURED_LOOKUP 대상, BM25 HEAVY) ---
# 조항·장·절·편·항·호·표·그림·번호 항목처럼 "정확한 문자열 참조"가 들어간 질의는
# 의미 검색(Dense)보다 키워드 매칭(BM25/Sparse)이 더 안정적이다.
# 예: "제43조", "제2장", "부칙", "별표 1", "3.2.1", "Section 4", "표 2"

# [BASE] 한국어 구조화 번호 참조 — 법령·규정·매뉴얼 공통
_KOREAN_STRUCT_REF = (
    r"제\s*\d+\s*조"                  # 제3조
    r"|제\s*\d+\s*[장절편항호]"       # 제2장 / 제3절 / 제4편 / 제1항 / 제2호
    r"|부칙"                          # 부칙
    r"|별표\s*\d+"                    # 별표 1
    r"|\d+\.\d+[\.\d]*\s*[장절항호]?" # 3.2.1 / 2.1절
)

# [BASE] 영문·문서 일반 구조 참조
_DOC_REF = (
    r"서식\s*\d+|양식\s*\d+|표\s*\d+|그림\s*\d+"
    r"|[Ss]ection\s*\d+|[Cc]hapter\s*\d+"
)

_STRUCTURED_REF_PATTERN = re.compile(f"{_KOREAN_STRUCT_REF}|{_DOC_REF}")

# --- 절차·방법 질의 패턴 (PROCEDURE 대상, DENSE HEAVY) ---
# "어떻게", "방법", "단계", "순서"처럼 수행 절차를 묻는 질의는
# 정확한 토큰 일치보다 의미 기반 검색이 유리하다.
_PROCEDURE_PATTERN = re.compile(
    r"어떻게|방법|절차|신청|청구|접수|제출|가입"
    r"|하려면|하려고|해야\s*하나|순서|과정|단계"
    r"|how\s+to|steps?|process|procedure"
)

# --- 비교 질의 패턴 (COMPARISON 대상, DENSE HEAVY) ---
# 둘 이상을 비교하는 질의. 하이브리드 검색(DENSE_HEAVY)으로 넓게 모으고 비교 프롬프트로
# LLM이 문맥에서 비교한다. (쿼리 분해는 제거 — SOTA상 닫힌 코퍼스엔 과설계, verification-redesign 참조)
_COMPARISON_PATTERN = re.compile(
    r"비교|차이|다른\s*점|구별|구분|vs|versus|다른가|뭐가\s*달라"
    r"|differ|compare|distinction"
)

# --- 해석·판단 질의 패턴 (INTERPRETATION 대상, DENSE HEAVY) ---
# 의미 설명, 적용 여부, 자격/대상 충족 여부를 묻는 질의는
# 단순 문자열 매칭보다 문맥 기반(Dense) 검색이 적합하다.
# yes/no 의도("~되나요?", "가능한가요?") 포함.

# [BASE] 한국어 일반 해석·적용 판단 어휘
_INTERPRETATION_BASE = (
    r"해석|의미|뜻|판단|적용|해당|간주|해석상"
    r"|경우에|경우는|때는|하면"
    r"|되나요|가능한가요|가능합니까|할\s*수\s*있나"
    r"|허용되나|인정되나|포함되나|대상인가"
    r"|what\s+does|mean|apply|eligible|qualify"
)

# [DOMAIN extension] 보험·금융·급여 지급 맥락에서 자주 나오는 해석 어휘
# 다른 도메인 운영 시 이 블록은 제거하거나 해당 도메인 어휘로 교체.
# 예) 의료: r"증상|병명|진단|처방|부작용|권장되나"
# 예) 제품 매뉴얼: r"지원되나|호환되나|설치되나|작동하나"
_INTERPRETATION_INSURANCE = (
    r"보장되나|보장이\s*되나|나오나요|받을\s*수\s*있나|면책|지급되나"
)

_INTERPRETATION_PATTERN = re.compile(
    _INTERPRETATION_BASE + "|" + _INTERPRETATION_INSURANCE
)


def classify_query(query: str) -> RouteResult:
    """쿼리를 분석해서 검색 전략과 질의 유형을 결정한다."""
    has_structured_ref = bool(_STRUCTURED_REF_PATTERN.search(query))
    has_procedure      = bool(_PROCEDURE_PATTERN.search(query))
    has_comparison     = bool(_COMPARISON_PATTERN.search(query))
    has_interpretation = bool(_INTERPRETATION_PATTERN.search(query))

    # 구조적 참조만 있고 의미 질의가 없으면 → BM25 위주 (x3/x8)
    # "제43조" 같은 pure lookup은 BM25 후보를 8배로 늘려야 정확 매칭 확률이 올라감.
    # Dense는 보조(x3)로만 둬서 유사 항목이 혼입되는 것을 방지.
    if has_structured_ref and not (has_interpretation or has_procedure or has_comparison):
        return RouteResult(
            strategy=SearchStrategy.BM25_HEAVY,
            query_type=QueryType.STRUCTURED_LOOKUP,
            dense_factor=3, bm25_factor=8,
        )

    # 구조적 참조 + 의미 질의 혼합 → 하이브리드 균등 (x6/x6)
    if has_structured_ref:
        query_type = QueryType.INTERPRETATION
        if has_procedure:
            query_type = QueryType.PROCEDURE
        elif has_comparison:
            query_type = QueryType.COMPARISON
        return RouteResult(
            strategy=SearchStrategy.HYBRID,
            query_type=query_type,
            dense_factor=6, bm25_factor=6,
        )

    # 의미 질의(비교/절차/해석) → Dense 위주 (x8/x3)
    # Dense를 8배로 늘려 의미적으로 관련된 문서를 충분히 확보하고, BM25는 보조(x3).
    if has_comparison:
        return RouteResult(
            strategy=SearchStrategy.DENSE_HEAVY,
            query_type=QueryType.COMPARISON,
            dense_factor=8, bm25_factor=3,
        )

    # 절차 질의
    if has_procedure:
        return RouteResult(
            strategy=SearchStrategy.DENSE_HEAVY,
            query_type=QueryType.PROCEDURE,
            dense_factor=8, bm25_factor=3,
        )

    # 해석 질의
    if has_interpretation:
        return RouteResult(
            strategy=SearchStrategy.DENSE_HEAVY,
            query_type=QueryType.INTERPRETATION,
            dense_factor=8, bm25_factor=3,
        )

    # 기본: 하이브리드 + SIMPLE_FACT (패턴 미매치 시)
    return RouteResult(
        strategy=SearchStrategy.HYBRID,
        query_type=QueryType.SIMPLE_FACT,
        dense_factor=6, bm25_factor=6,
    )
