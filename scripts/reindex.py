"""청커·전처리를 바꾼 뒤 **문서 단위로 재색인** (chunk → embed).

용법 (api 컨테이너 안 — BGE-M3 가 여기 있다):
    python /app/scripts/reindex.py --dry-run
    python /app/scripts/reindex.py R05
    python /app/scripts/reindex.py --all

⚠ **`if __name__ == "__main__":` 가드를 절대 지우지 마라.** 실측으로 데였다(2026-09-10):
`embed_document` 는 `qdrant.upload_points(..., parallel=4)` 로 업로드를 병렬화하는데, 이게
**프로세스를 띄우면서 자식이 `__main__` 모듈을 다시 import** 한다. 가드가 없으면 자식마다
스크립트 본문이 통째로 재실행돼 재귀 팬아웃이 난다. 실제 피해:
    · chunk_document 가 **8중 동시 실행** → 같은 문서 청크가 8배(332 → 2,656)
    · 자식들의 `qdrant.delete(document_id=…)` 가 서로의 업로드를 지워 포인트 소실
    · 프로세스마다 BGE-M3 를 들고 있어 api 컨테이너(3g) OOM-kill(exit 137)
상태 로그가 범인을 지목했다 — `43→32` 8줄이 **같은 초에, 모두 from_status=43** 으로 찍혔다.
순차 재시도였다면 두 번째는 32 를 읽었을 것이다. 즉 동시 실행이다.

⚠ 재색인 전에 `make backup`. 이 스크립트는 문서별로 delete + insert 한다.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 벤치 재료(관악구 공고문) — 약관이 아니라 재색인 대상이 아니다.
BENCH_DOCS = {"B005", "D14101", "D14102", "D14103"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("docs", nargs="*", help="document_id (없으면 --all 필요)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="대상만 출력")
    a = ap.parse_args()
    if not a.docs and not a.all:
        ap.error("document_id 를 주거나 --all")

    from sqlalchemy import text

    from v1.config import task_session

    with task_session() as db:
        rows = db.execute(text(
            "SELECT service_code, document_id, document_name FROM tb_document_extract "
            "ORDER BY document_id")).all()
    rows = [r for r in rows if r[1] not in BENCH_DOCS]
    if a.docs:
        rows = [r for r in rows if r[1] in set(a.docs)]
    if not rows:
        print("  대상 없음"); return 1

    print(f"  대상 {len(rows)}문서: {', '.join(r[1] for r in rows)}")
    if a.dry_run:
        return 0

    from v1.task.chunk import chunk_document
    from v1.task.embed import embed_document

    ok = fail = 0
    for sc, did, name in rows:
        t0 = time.time()
        prev = {"service_code": sc, "document_id": did, "document_name": name}
        try:
            chunk_document.run(prev)
            embed_document.run(prev)
        except Exception as e:
            fail += 1
            print(f"  ❌ {did:<14}{type(e).__name__}: {e}", flush=True)
            continue
        with task_session() as db:
            n, ncap = db.execute(text(
                "SELECT count(*), count(*) FILTER (WHERE chunk_type='table' AND content !~ '^\\|') "
                "FROM tb_document_chunks WHERE service_code=:sc AND document_id=:d"),
                {"sc": sc, "d": did}).first()
        ok += 1
        print(f"  ✅ {did:<14}청크 {n:>5} · 캡션표 {ncap:>3} · {time.time() - t0:>4.0f}s", flush=True)
    print(f"\n  완료 {ok} · 실패 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":          # ← 위 경고 참조. 지우면 프로세스가 재귀 폭발한다.
    sys.exit(main())
