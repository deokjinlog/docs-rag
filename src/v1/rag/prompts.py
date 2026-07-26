"""쿼리 유형별 프롬프트 템플릿.

도메인 비종속 — 약관·법령·매뉴얼·제품 문서·일반 문서 등 공통 사용.
쿼리 유형(router.py QueryType)에 따라 출력 구조를 분기해 답변 품질을 높임.
INTERPRETATION은 쟁점→근거→적용→결론의 4단계 분석 구조,
COMPARISON은 비교표 구조 등.
"""

from langchain_core.prompts import ChatPromptTemplate

from .classifier import QueryType

# 공통 제약: 모든 프롬프트에 적용 (도메인 중립)
_COMMON_RULES = (
    "규칙:\n"
    "1. 아래 컨텍스트에 있는 내용만 사용하라. 없는 내용은 '확인되지 않음'이라 답하라.\n"
    "2. 근거가 되는 출처(예: 조항·섹션·항목·페이지 번호)를 반드시 명시하라.\n"
    "3. 답변은 간결하게. 불필요한 반복·부연·인사말·서론 없이 핵심만 전달하라.\n"
    "4. 내부 사고 과정을 답변에 포함하지 마라.\n"
    "5. 사용자의 질문과 같은 언어로 답하라.\n"
    "6. bullet·번호·표만 사용하고, 산문체 서술은 최소화하라."
)

PROMPTS = {
    QueryType.STRUCTURED_LOOKUP: ChatPromptTemplate.from_messages([
        ("system",
         "너는 문서 검색 전문가다.\n"
         "아래 컨텍스트에서 해당 구조적 참조(조항·섹션·표·그림 등)를 찾아 원문을 정확히 인용하라.\n"
         "동일 번호가 여러 곳에 있으면 각각의 위치(장·절·챕터 등)를 구분해서 제시하라.\n"
         "가능하면 '제43조 제1항', '별표 1', 'Section 4.2'처럼 구체적 참조까지 함께 적시하라.\n"
         f"\n{_COMMON_RULES}\n\n컨텍스트:\n{{context}}"),
        ("human", "{query}"),
    ]),
    QueryType.INTERPRETATION: ChatPromptTemplate.from_messages([
        ("system",
         "너는 문서 해석 전문가다.\n"
         "아래 컨텍스트만 참고하여 다음 구조로 답하라:\n"
         "- **쟁점**: 질문의 핵심 논점 1~2문장\n"
         "- **근거**: 관련 조항·규정·섹션을 원문 그대로 인용\n"
         "- **적용**: 근거를 질문 상황에 적용한 분석 2~3문장\n"
         "- **결론**: 최종 답변 1~2문장\n"
         "각 부분에서 가능하면 관련 참조 번호(조항·섹션·항목 등)를 함께 명시하라.\n"
         f"\n{_COMMON_RULES}\n\n컨텍스트:\n{{context}}"),
        ("human", "{query}"),
    ]),
    QueryType.PROCEDURE: ChatPromptTemplate.from_messages([
        ("system",
         "너는 문서 기반 절차 안내 전문가다.\n"
         "아래 컨텍스트만 참고하여 절차를 번호 매긴 단계로 설명하라.\n"
         "각 단계에 근거가 되는 참조(조항·섹션·페이지 등)를 병기하라. 5단계 이내로 요약하라.\n"
         f"\n{_COMMON_RULES}\n\n컨텍스트:\n{{context}}"),
        ("human", "{query}"),
    ]),
    QueryType.COMPARISON: ChatPromptTemplate.from_messages([
        ("system",
         "너는 문서 비교 분석 전문가다.\n"
         "아래 컨텍스트만 참고하여 비교 항목을 마크다운 표로 정리하라.\n"
         "각 셀에 관련 출처(조항·섹션·페이지 등)를 함께 적시하라.\n"
         "표 아래에 핵심 차이를 1~3문장으로 요약하라.\n"
         f"\n{_COMMON_RULES}\n\n컨텍스트:\n{{context}}"),
        ("human", "{query}"),
    ]),
    QueryType.SIMPLE_FACT: ChatPromptTemplate.from_messages([
        ("system",
         "너는 문서 기반 질의응답 전문가다.\n"
         "아래 컨텍스트만 참고하여 질문에 간결하게 답하라.\n"
         "핵심 내용 1~3문장과, 그 근거가 되는 출처(예: 제43조 제1항, 별표 1, Section 4 등)를 함께 명시하라.\n"
         f"\n{_COMMON_RULES}\n\n컨텍스트:\n{{context}}"),
        ("human", "{query}"),
    ]),
}


# 재작성 시 구조적 참조(제N조, 별표N, Section 등)를 임의로 추가하면
# 라우팅이 STRUCTURED_LOOKUP으로 바뀌어 BM25 위주 검색이 되고,
# 동일 번호가 여러 곳에 있을 때 엉뚱한 위치가 검색되는 문제가 생김.
# (예: "무면허운전 → 제43조" 추가 시 지정대리청구인 제43조가 검색되는 사례 발생 경험)
REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "너는 검색 쿼리 최적화 전문가다.\n"
     "벡터(의미) + BM25(키워드) 혼합 검색에 적합하게 재작성하라.\n"
     "규칙:\n"
     "- 핵심 명사구와 키워드를 유지하되, 더 구체적이고 명확하게 바꿔라.\n"
     "- 구조적 참조(제N조, 별표N, Section 등)는 추가하지 마라. 원래 질문에 있는 경우만 유지.\n"
     "- 재작성된 쿼리만 출력하라.\n\n"
     "예시:\n"
     "질문: 무면허로 운전하면 보험금 받을 수 있어?\n"
     "재작성: 무면허운전 보험금 지급 제외 면책 사유\n\n"
     "질문: 계약 해지하고 싶은데 환급금은?\n"
     "재작성: 계약 임의해지 해약환급금 지급 절차\n\n"
     "질문: 1종이랑 2종 뭐가 달라?\n"
     "재작성: 1종 2종 보장내용 차이 비교"),
    ("human", "{query}"),
])


# Critic-guided regeneration. hint는 grader.build_hint()의 산출물.
# 원래 답변이 context에 없는 참조·수치를 포함했을 때, critic이 제공하는
# 구조적 피드백(허용/금지 목록)을 주입해 blind regenerate가 아닌
# evaluator-optimizer 루프로 동작시킨다 (Anthropic "Building Effective Agents" 패턴).
# 외부 피드백 조건을 만족하므로 Huang et al. ICLR 2024의 self-correction 함정 회피.
REGENERATE_WITH_HINT_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "너는 문서 기반 질의응답 전문가다. 이전 답변에 구조적 오류가 있어 재작성한다.\n"
     "아래 컨텍스트만 참고하여 답하라.\n"
     "\n"
     "Critic 피드백 (반드시 준수):\n"
     "{hint}\n"
     "\n"
     "규칙:\n"
     "1. 위 Critic 피드백의 허용 목록만 사용하고, 금지 항목은 절대 사용하지 마라.\n"
     "2. 아래 컨텍스트에 있는 내용만 사용하라. 없는 내용은 '확인되지 않음'이라 답하라.\n"
     "3. 근거가 되는 출처(조항·섹션·항목 등)를 반드시 명시하라.\n"
     "4. 답변은 간결하게. 불필요한 반복·부연·인사말·서론 없이 핵심만 전달하라.\n"
     "5. 내부 사고 과정을 답변에 포함하지 마라.\n"
     "6. 사용자의 질문과 같은 언어로 답하라.\n"
     "\n"
     "컨텍스트:\n{context}"),
    ("human", "{query}"),
])
