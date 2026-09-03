"""BGE-M3 임베딩.
local_files_only=True로 HF Hub 접속 없이 로컬 모델만 사용.
싱글턴으로 로드하여 태스크 간 모델 재로딩 방지.
"""

import threading

from sentence_transformers import SentenceTransformer

from ..config import EMBEDDING_CONFIG

_model = None
_model_lock = threading.Lock()


def get_embedding_model() -> SentenceTransformer:
    """BGE-M3 싱글톤 (double-checked locking).

    Why 락: celery 워커가 `--pool=threads --concurrency=N`이라 락 없는 lazy init은
    N개 스레드가 `_model is None`을 동시에 통과해 모델을 N벌 로드한다. BGE-M3는
    벌당 ~2.2GB라 celery의 mem_limit 3g를 즉시 넘겨 OOM-kill → 재시작 루프.
    실측(2026-09-03): concurrency=4에서 3GiB/3GiB 고착·RestartCount 9.
    paddle/server.py `_get_engine()`이 같은 이유로 이미 쓰는 패턴.
    준비는 1번, 사용은 병렬 — encode 자체는 stateless라 락 밖에서 동시 실행된다.
    """
    global _model
    if _model is not None:      # fast path — 이미 준비됨, 락 없이 바로 return
        return _model
    with _model_lock:           # 첫 호출만 락 잡음. 나머지 동시 요청은 여기서 대기
        if _model is None:      # 락 획득 시점엔 다른 스레드가 먼저 만들었을 수 있음 → 재확인
            _model = SentenceTransformer(EMBEDDING_CONFIG["model_path"], local_files_only=True)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_embedding_model()
    return model.encode(texts, normalize_embeddings=True).tolist()


def embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]


def count_tokens(texts: list[str]) -> list[int]:
    """BGE-M3 토크나이저 기준 토큰 수 계산."""
    tokenizer = get_embedding_model().tokenizer
    return [len(tokenizer.encode(t, add_special_tokens=False)) for t in texts]
