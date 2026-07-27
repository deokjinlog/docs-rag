"""Qdrant / LLM / BM25 / Reranker / Embedding 설정 dict.

5개 외부 자원의 연결 파라미터를 한 곳에. dict 키는 호출 측에서 ** 언패킹
또는 직접 인덱싱으로 사용 — 별도 dataclass 안 만든 이유는 옵션이 자주
튜닝 대상(hnsw_ef, max_tokens, safety_margin 등)이라 dict가 더 가벼움.

vector_size=1024 / sparse_vector_name="content-bm25"는 컬렉션 스키마와
embed.py·router.py 검색 코드 3곳이 동시에 합의해야 하는 값. 변경 시
컬렉션 재생성 필요 (CLAUDE.md "연쇄 수정" 섹션 참조).
"""

import os

QDRANT_CONFIG = {
    "host": os.environ["QDRANT_HOST"],
    "port": int(os.environ["QDRANT_PORT"]),
    "grpc_port": int(os.environ.get("QDRANT_GRPC_PORT", "6334")),
    "collection_name": os.environ["QDRANT_COLLECTION"],
    "vector_size": 1024,
    "distance": "Cosine",
    # 검색 시 후보 탐색 범위. 클수록 recall↑, 지연↑. 64/128/256으로 올려가며 튜닝.
    "hnsw_ef": 128,
}

# 보험 약관 전용 컬렉션 (관계형 SQL 계층과 분리된 RAG 해석 계층). 컬렉션명에 임베딩
# 모델·차원을 박아둔다 — 나중에 모델을 바꾸면(bge-m3→e5 등) 벡터 공간이 달라 구 컬렉션과
# 섞으면 안 되는데, 이름이 곧 스키마라 실수로 섞는 걸 막는다. 이 상수 한 곳에서만 참조.
INSURANCE_COLLECTION = "insurance_bge_m3_1024"

LLM_CONFIG = {
    "base_url": os.environ["LLM_BASE_URL"],
    "model": os.environ["LLM_MODEL"],
    "api_key": os.environ.get("LLM_API_KEY", "no-key"),
    "options": {"temperature": 0.0, "top_p": 0.95, "max_tokens": 2048, "seed": 42},
    # max_context는 vLLM --max-model-len 과 반드시 일치. 로컬 8GB + Qwen3-4B-AWQ는
    # 가중치가 작아 KV 여유가 충분 → 8192 가능(7B는 KV 부족으로 3072가 상한이라 RAG 근거가
    # 잘려 품질↓ 이었음). max_tokens+context+프롬프트 합이 이 값을 넘으면 vLLM이 400.
    "max_context": 8192,
    "safety_margin": 256,
}

BM25_CONFIG = {
    "sparse_vector_name": "content-bm25",
    "modifier": "idf",
}

RERANKER_CONFIG = {
    "model": os.environ["RERANKER_MODEL"],
    "use_fp16": True,
}

EMBEDDING_CONFIG = {
    "model_path": os.environ["EMBEDDING_MODEL_PATH"],
}
