"""원본 PDF 경로 해석.

**왜 필요한가** — `tb_document_extract.document_path` 는 **등록 시점** 경로(`data/input/…`)를
가리키는데, `extract.py` 가 처리 후 파일을 `data/finished/` 로 옮긴다. 그래서 기록된 경로만
보면 색인된 22문서가 전부 "원본 없음"으로 보인다. 실제로 그렇게 오진했다 — 그 오진 위에서
"SVG 로 페이지를 재구성하자 / 다단 정답이 문서 1개뿐이다" 같은 설계 결정이 내려질 뻔했다.
실제로는 `data/finished` 33건 · `data/error` 7건에 **전부** 있었고, 다단 정답도 5문서 305p 다.

경로를 고치는 대신 **찾는 함수**를 둔다. 이동은 파이프라인의 정상 동작이고, 어느 단계에
있느냐가 곧 상태이기 때문이다(input=대기/처리중, finished=완료, error=실패).
M2 에서 `source_path_resolved`·`sha256` 컬럼으로 굳힌다.
"""
import hashlib
from pathlib import Path

from ..config import DATA_DIR, ERROR_DIR, FINISHED_DIR, INPUT_DIR

# 찾는 순서 = 파이프라인 진행 순서. 같은 이름이 여러 곳에 있으면 **가장 이른 단계**를 준다
# (재등록 중인 파일이 있으면 그게 현재 처리 대상이므로).
SEARCH_ORDER = ("input", "finished", "error")
_DIRS = {"input": INPUT_DIR, "finished": FINISHED_DIR, "error": ERROR_DIR}


def resolve_source(document_name: str) -> tuple[Path | None, str | None]:
    """`(경로, 어느 단계)`. 못 찾으면 `(None, None)`.

    `document_name` 은 파일명만 받는다(경로 포함 금지 — extract.py 와 같은 규칙).
    """
    safe = Path(document_name or "").name
    if not safe:
        return None, None
    for stage in SEARCH_ORDER:
        p = _DIRS[stage] / safe
        if p.is_file():
            return p, stage
    return None, None


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    """파일 내용 지문. 이름이 달라도 같은 문서면 같은 값 —
    같은 약관을 두 번 올려 청크가 중복 색인되는 것을 막는 키다(M2 에서 컬럼으로)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def rel_to_data(path: Path) -> str:
    """로그·리포트용 짧은 경로. DATA_DIR 밖이면 그대로 돌려준다."""
    try:
        return str(path.relative_to(DATA_DIR))
    except ValueError:
        return str(path)
