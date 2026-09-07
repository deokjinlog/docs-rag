"""공유 자원 싱글톤 — Qdrant client / LLM / CrossEncoder reranker.

Module import 시점에 1회 초기화. CrossEncoder 모델 로드가 무거우므로 (~1GB)
프로세스당 한 번만. router·search 등 여러 모듈이 import해도 동일 인스턴스 공유.

테스트에서 mock 필요 시 `patch("src.v1.rag.clients.llm")` 등으로 단일 진입점 mocking.
"""
from __future__ import annotations

import re

from langchain_openai import ChatOpenAI
from qdrant_client import QdrantClient
from sentence_transformers import CrossEncoder

from ..config import LLM_CONFIG, QDRANT_CONFIG, RERANKER_CONFIG

qdrant = QdrantClient(
    host=QDRANT_CONFIG["host"],
    port=QDRANT_CONFIG["port"],
    grpc_port=QDRANT_CONFIG["grpc_port"],
    prefer_grpc=True,
)

llm = ChatOpenAI(
    model=LLM_CONFIG["model"],
    base_url=LLM_CONFIG["base_url"],
    api_key=LLM_CONFIG["api_key"],
    temperature=LLM_CONFIG["options"]["temperature"],
    top_p=LLM_CONFIG["options"]["top_p"],
    max_tokens=LLM_CONFIG["options"]["max_tokens"],
    seed=LLM_CONFIG["options"]["seed"],
)

reranker = CrossEncoder(RERANKER_CONFIG["model"], local_files_only=True)

# Qwen3 등 reasoning 모델이 출력하는 내부 사고 과정 제거.
# 호출처 drift 방지 위해 invoke_clean() 으로 묶음 — 모든 LLM 호출은 이 함수 경유 권장.
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# **닫히지 않은** think 블록. max_tokens 에 걸려 잘리면 `</think>` 가 영영 안 나온다.
# 그러면 위 non-greedy 패턴이 매칭에 실패해 **추론 전문이 그대로 통과**한다.
# 실측(2026-09-07): CRAG 재작성이 5,000자짜리 영어 추론 덤프를 검색 쿼리로 써서
# query_embed 가 200ms → 8,181ms (40배)로 뛰고 결과는 잡음이었다. 사용자 답변 경로에서
# 같은 일이 나면 추론 과정이 그대로 노출된다.
THINK_UNCLOSED_RE = re.compile(r"<think>.*\Z", re.DOTALL)


def invoke_clean(messages) -> str:
    """LLM 호출 + reasoning think 태그 제거 단일 진입점.

    Qwen3·DeepSeek R1 같은 reasoning 모델은 답변 앞에 `<think>...</think>` 로
    내부 사고 과정을 같이 출력함. 사용자한테 노출되면 안 되므로 strip.
    이 함수를 모든 LLM 호출의 단일 진입점으로 두면 drift 방지.

    닫힌 블록 → 열린 채 잘린 블록 순으로 두 번 지운다. 후자를 빼먹으면 토큰 한도에
    걸린 응답이 통째로 새어나간다(위 THINK_UNCLOSED_RE 주석의 실측 사례).
    추론만 하다 잘린 경우 결과가 빈 문자열이 될 수 있으므로 **호출처가 빈 값을 처리**해야 한다.
    """
    raw = llm.invoke(messages).content
    return THINK_UNCLOSED_RE.sub("", THINK_RE.sub("", raw)).strip()
