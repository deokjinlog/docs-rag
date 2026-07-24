#!/usr/bin/env bash
# 로컬 실행용 모델 가중치 다운로드 → ./model/
#
# 코드가 local_files_only=True 로 로드하므로 (HF 접속 없이 로컬만 사용) 사전 다운로드가 필수다.
# 필요: pip install -U "huggingface_hub[cli]"   (또는 uv pip install)
#
# 사용:
#   bash scripts/download_models.sh
#   LLM_REPO=Qwen/Qwen2.5-3B-Instruct-AWQ bash scripts/download_models.sh   # LLM만 교체
set -euo pipefail

EMBED_REPO="${EMBED_REPO:-BAAI/bge-m3}"
RERANK_REPO="${RERANK_REPO:-BAAI/bge-reranker-v2-m3}"
# 소형 LLM (AWQ). 임베더·리랭커는 CPU라 GPU는 vLLM 독점 → 3B 넉넉, 더 작게 1.5B도 OK.
LLM_REPO="${LLM_REPO:-Qwen/Qwen2.5-3B-Instruct-AWQ}"

mkdir -p model

echo "▶ 임베더  : $EMBED_REPO → model/BGE-M3"
huggingface-cli download "$EMBED_REPO" --local-dir model/BGE-M3

echo "▶ 리랭커  : $RERANK_REPO → model/bge-reranker-v2-m3"
huggingface-cli download "$RERANK_REPO" --local-dir model/bge-reranker-v2-m3

echo "▶ LLM     : $LLM_REPO → model/llm"
huggingface-cli download "$LLM_REPO" --local-dir model/llm

echo "✓ 완료 → ./model/  (이제 docker compose up 가능)"
