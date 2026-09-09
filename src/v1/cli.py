"""인제스트 CLI — 웹서버 없이 문서를 등록하고 완료를 기다린다.

**왜 필요한가** (D1): 메모리 예산 때문에 `ingest` 프로필에는 `api` 가 없다(넣으면 11.5Gi 로
상한을 넘는다). 그런데 등록은 지금까지 `POST /documents` 뿐이었다 — 즉 색인을 하려면
서빙용 웹서버를 함께 띄워야 했다. 인제스트가 웹서버에 의존할 이유가 없으므로 같은 일을
하는 진입점을 CLI 로 둔다. 하는 일은 엔드포인트와 동일하다(문서 행 생성 + 체인 발행).

`--wait` 는 상태가 11(완료) 또는 9x(에러)가 될 때까지 기다린다. **한 건씩 순차로** 넣기
위한 것이다 — 8건을 한꺼번에 넣었더니 JVM 이 병렬로 떠서 스택이 통째로 죽었다(실측 4회).

용법 (celery 컨테이너 안에서, working_dir=/app/src):
    python -m v1.cli register --service 01 --id B011 --name "약관.pdf" --wait
    python -m v1.cli status --prefix B0
"""
import argparse
import sys
import time

from .config import StatusCode, task_session
from .repository import DocumentRepository


def _publish(service_code: str, document_id: str, document_name: str) -> str | None:
    from v1.task.extract import extract_pdf
    from v1.task.ocr import ocr_images
    from v1.task.chunk import chunk_document
    from v1.task.embed import embed_document

    chain = (extract_pdf.s(service_code, document_id, document_name)
             | ocr_images.s() | chunk_document.s() | embed_document.s())
    return chain.apply_async().id


def _status(prefix: str) -> list[tuple[str, str]]:
    with task_session() as db:
        rows = db.execute(
            __import__("sqlalchemy").text(
                "SELECT document_id, status_code FROM tb_document_status "
                "WHERE document_id LIKE :p ORDER BY document_id"),
            {"p": f"{prefix}%"},
        ).all()
    return [(r[0], r[1]) for r in rows]


def cmd_register(a) -> int:
    with task_session() as db:
        DocumentRepository(db).create(a.service, a.id, a.name, a.path)
    tid = _publish(a.service, a.id, a.name)
    print(f"  등록 {a.service}/{a.id}  task={tid}  {a.name}", flush=True)
    if not a.wait:
        return 0

    t0 = time.time()
    last = None
    while time.time() - t0 < a.timeout:
        cur = dict(_status(a.id)).get(a.id)
        if cur != last:
            print(f"    [{time.time() - t0:5.0f}s] status={cur}", flush=True)
            last = cur
        if cur == a.until:
            print(f"  ✅ status={cur} 도달 ({time.time() - t0:.0f}s)", flush=True)
            return 0
        if cur and cur.startswith("9"):
            print(f"  ❌ 에러 status={cur}", file=sys.stderr, flush=True)
            return 1
        time.sleep(5)
    print(f"  ⏱ 타임아웃 ({a.timeout}s) status={last}", file=sys.stderr, flush=True)
    return 2


def cmd_status(a) -> int:
    for did, st in _status(a.prefix):
        print(f"  {did}  {st}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="v1.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register", help="문서 등록 + 체인 발행")
    r.add_argument("--service", default="01")
    r.add_argument("--id", required=True)
    r.add_argument("--name", required=True)
    r.add_argument("--path", default=None)
    r.add_argument("--wait", action="store_true", help="목표 상태/에러(9x)까지 대기")
    # OCR 은 별도 큐라 ingest 프로필만 떠 있으면 21(추출완료)에서 멈춘다. 단계별로 기다리려면
    # 목표 상태를 바꿀 수 있어야 한다 — 그래서 --until 이 있다(기본 11=전체완료).
    r.add_argument("--until", default=StatusCode.COMPLETE_ALL, help="이 상태가 되면 성공 종료")
    r.add_argument("--timeout", type=int, default=3600)
    r.set_defaults(fn=cmd_register)

    s = sub.add_parser("status", help="상태 조회")
    s.add_argument("--prefix", default="")
    s.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
