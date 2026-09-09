"""Utils 패키지 — 데이터 파이프라인 유틸 (청킹, 전처리, 임베딩).

서빙 파이프라인 헬퍼(sibling 복원·토큰 예산·검색)는 rag/ 패키지에 위치.
"""

from .chunker import chunk_markdown, to_json
from .preprocess import normalize_whitespace, clean_text

# ⚠ **embedding 은 여기서 re-export 하지 않는다.** `.embedding` 은 모듈 최상단에서
# sentence_transformers 를 import 하므로, 여기 두면 `v1.utils.<아무거나>` 를 가져오는 것만으로
# BGE-M3 스택이 딸려온다. CLAUDE.md 의 규칙이 정확히 이걸 금지한다 —
# "heavy 자원 모듈은 사용처가 submodule 에서 직접 import (unit test 가 의도치 않게
#  모델 로드 트리거하는 회귀 방지)". 실제로 순수 규칙 모듈(triage)을 스택 없이 테스트하려다
# 이 re-export 때문에 막혔다. 소비자는 `from ..utils.embedding import ...` 로 직접 가져간다.

__all__ = [
    "chunk_markdown", "to_json",
    "normalize_whitespace", "clean_text",
]
