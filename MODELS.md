# 로컬 모델 재다운로드

`model/` 은 용량 정리(2026-08-24)로 삭제됨(원래 6.8G, gitignore 대상이라 repo엔 없음).
서빙 재개 시 아래로 복구한다. 원천 코퍼스(`data/`)와 무관 — 모델은 HF에서 다시 받으면 됨.

| 로컬 경로 | HF repo id | 크기 |
|---|---|---|
| `model/BGE-M3` | `BAAI/bge-m3` | ~2.2G |
| `model/bge-reranker-v2-m3` | `BAAI/bge-reranker-v2-m3` | ~2.2G |
| `model/Qwen3-4B-AWQ` | `Qwen/Qwen3-4B-AWQ` | ~2.5G |

## 재다운로드 (uv, pip 불필요)

```bash
uv run --with huggingface_hub python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download("BAAI/bge-m3",              local_dir="model/BGE-M3")
snapshot_download("BAAI/bge-reranker-v2-m3",  local_dir="model/bge-reranker-v2-m3")
snapshot_download("Qwen/Qwen3-4B-AWQ",        local_dir="model/Qwen3-4B-AWQ")
PY
```

의존성(`.venv`)도 함께 삭제됨 → `uv sync` 로 복구.
