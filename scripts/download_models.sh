#!/usr/bin/env bash
# 로컬 실행용 모델 가중치 다운로드 → ./model/
#
# 코드가 local_files_only=True 로 로드하므로 (HF 접속 없이 로컬만 사용) 사전 다운로드가 필수다.
# 필요: pip install -U "huggingface_hub[cli]"   (또는 uv pip install) → `hf` 명령 제공
#
# 사용:
#   bash scripts/download_models.sh
#   LLM_REPO=Qwen/Qwen2.5-3B-Instruct-AWQ bash scripts/download_models.sh   # LLM만 교체
set -euo pipefail

EMBED_REPO="${EMBED_REPO:-BAAI/bge-m3}"
RERANK_REPO="${RERANK_REPO:-BAAI/bge-reranker-v2-m3}"
# 로컬 LLM (AWQ). 임베더·리랭커는 CPU라 GPU는 vLLM 독점.
# 8GB RAG 스윗스팟은 Qwen3-4B — 가중치가 작아 KV 여유가 커서 컨텍스트 8192 확보.
# (7B는 KV 부족으로 컨텍스트가 3072로 줄어 RAG 근거가 잘려 품질↓ 이었음 — 실측.)
LLM_REPO="${LLM_REPO:-Qwen/Qwen3-4B-AWQ}"

mkdir -p model

echo "▶ 임베더  : $EMBED_REPO → model/BGE-M3"
hf download "$EMBED_REPO" --local-dir model/BGE-M3

echo "▶ 리랭커  : $RERANK_REPO → model/bge-reranker-v2-m3"
hf download "$RERANK_REPO" --local-dir model/bge-reranker-v2-m3

# LLM은 repo 이름 그대로 폴더명으로 (자기설명적). docker-compose.yml vllm 마운트도 이 경로와 일치해야 함.
LLM_DIR="model/$(basename "$LLM_REPO")"
echo "▶ LLM     : $LLM_REPO → $LLM_DIR"
hf download "$LLM_REPO" --local-dir "$LLM_DIR"

echo "✓ 완료 → ./model/  (docker-compose.yml vllm 볼륨을 $LLM_DIR 에 맞췄는지 확인 후 docker compose up)"
