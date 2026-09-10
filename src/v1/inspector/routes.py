"""Inspector — 파싱 결과를 사람이 눈으로 판정하는 읽기 전용 화면.

**왜 만드나.** 지금까지의 파싱 검증은 전부 숫자였다. 글자 커버리지·조 보존율·dup@10 은
*무엇이* 얼마나 어긋났는지는 말해주지만 *그게 문제인지*는 말해주지 못한다. 실제로 R10 은
커버리지 FAIL 인데 열어보니 누락의 대부분이 목차 점선이었다 — 지표는 조판 장식과 본문 손실을
구분하지 못한다. 그 판정은 사람만 할 수 있고, 사람이 하려면 **원본 페이지와 파싱 결과를
나란히 놓는 화면**이 있어야 한다.

이 화면이 막고 있던 것들:
  · SCAN 골든 30페이지 — 사람이 교정할 UI 없이는 정답 텍스트를 못 만든다(진짜 CER 의 전제)
  · 검색 골든의 `verified:false` 20문항 — 원문 대조를 해야 true 로 올린다
  · R10 의 FAIL 이 조판인가 본문인가

**읽기 전용이다.** 전부 GET 이고 DB 에 쓰지 않는다. 유일한 쓰기는 페이지 렌더 캐시인데,
그건 원본에서 언제든 다시 만들 수 있는 파생물이다(지워도 안전).
"""
from __future__ import annotations

import collections
import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..config import DATA_DIR, get_db
from ..utils.source import resolve_source
from .coverage import page_report
from .dup import duplicate_groups

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_DPI = 110                  # 조문 글자가 읽히는 최소치. 올리면 캐시 용량이 제곱으로 는다
CACHE_DIR = DATA_DIR / "output" / "pagecache"

# 문서당 중복군 계산은 수백 청크 스캔이라 페이지를 넘길 때마다 다시 하면 낭비다.
# 청크 수를 키에 넣어 재청킹하면 자동 무효화된다.
_DUP_CACHE: dict[tuple[str, int], tuple[dict[int, int], dict[int, float]]] = {}
_DUP_CACHE_MAX = 8


def _dup_for_doc(db: Session, sc: str, did: str) -> tuple[dict[int, int], dict[int, float]]:
    rows = db.execute(text(
        "SELECT id, content FROM tb_document_chunks "
        "WHERE service_code=:sc AND document_id=:d ORDER BY id"), {"sc": sc, "d": did}).all()
    key = (f"{sc}/{did}", len(rows))
    if key not in _DUP_CACHE:
        if len(_DUP_CACHE) >= _DUP_CACHE_MAX:
            _DUP_CACHE.pop(next(iter(_DUP_CACHE)))
        _DUP_CACHE[key] = duplicate_groups([(r[0], r[1]) for r in rows])
    return _DUP_CACHE[key]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def doc_list(request: Request, dup: int = Query(0), db: Session = Depends(get_db)):
    """문서 목록 — 청크·페이지 판별·V1 판정을 한 줄에 모은다.

    `?dup=1` 은 전 문서의 중복군을 계산한다. 8,850 청크 전량 스캔이라 느려서 기본은 꺼둔다 —
    켜고 끄는 걸 사람이 정하게 두는 편이, 목록이 항상 느린 것보다 낫다.
    """
    docs = db.execute(text("""
        SELECT e.service_code, e.document_id, e.document_name, e.total_pages,
               s.status_code, e.source_stage
        FROM tb_document_extract e
        LEFT JOIN tb_document_status s
               ON s.service_code = e.service_code AND s.document_id = e.document_id
        ORDER BY e.document_id""")).all()

    chunk_stat = collections.defaultdict(collections.Counter)
    for sc, did, ct, n in db.execute(text(
            "SELECT service_code, document_id, chunk_type, count(*) "
            "FROM tb_document_chunks GROUP BY 1,2,3")).all():
        chunk_stat[(sc, did)][ct or "text"] = n

    triage_stat = collections.defaultdict(collections.Counter)
    for sc, did, route, n in db.execute(text(
            "SELECT service_code, document_id, route, count(*) "
            "FROM tb_page_triage GROUP BY 1,2,3")).all():
        triage_stat[(sc, did)][route] = n

    # V1 파싱 판정 — 가장 최근 parse run 의 문서별 verdict
    verdict = {}
    last = db.execute(text(
        "SELECT run_id FROM tb_eval_run WHERE kind='parse' ORDER BY created_at DESC LIMIT 1")).scalar()
    if last:
        for item, detail, scores in db.execute(text(
                "SELECT item_id, detail_json, scores_json FROM tb_eval_item WHERE run_id=:r"),
                {"r": last}).all():
            verdict[item] = {"verdict": (detail or {}).get("verdict"),
                             "cov": (scores or {}).get("char_cov_median")}

    rows = []
    for sc, did, name, pages, status, stage in docs:
        cs, ts = chunk_stat[(sc, did)], triage_stat[(sc, did)]
        d = None
        if dup:
            groups, _ = _dup_for_doc(db, sc, did)
            total = sum(cs.values()) or 1
            d = {"groups": len(set(groups.values())), "chunks": len(groups),
                 "pct": round(100 * len(groups) / total, 1)}
        rows.append({
            "sc": sc, "did": did, "name": name, "pages": pages, "status": status,
            "stage": stage, "chunks": cs, "n_chunks": sum(cs.values()),
            "triage": ts, "n_triage": sum(ts.values()),
            "v1": verdict.get(did), "dup": d,
        })
    # Starlette 신 시그니처는 request 가 첫 인자다 — 구 시그니처(name 먼저)로 넘기면
    # 템플릿 이름 자리에 dict 가 들어가 "unhashable type: dict" 로 죽는다.
    return templates.TemplateResponse(request, "list.html", {
        "rows": rows, "dup_on": bool(dup),
        "run_id": last,
    })


@router.get("/docs/{sc}/{did}", response_class=HTMLResponse)
def doc_view(request: Request, sc: str, did: str, page: int = Query(1),
             db: Session = Depends(get_db)):
    """문서 뷰어 — 왼쪽 원본 페이지, 오른쪽 그 페이지에서 나온 청크.

    페이지를 축으로 잡은 이유: 사람이 내려야 하는 판정이 *"이 페이지 내용이 청크에 다
    들어갔나 · 순서가 맞나 · 표가 살았나"* 이고, 그건 페이지↔청크 대조다. V1 의 글자
    커버리지가 숫자로 재는 바로 그 비교를 화면으로 옮긴 것.
    """
    meta = db.execute(text(
        "SELECT document_name, total_pages FROM tb_document_extract "
        "WHERE service_code=:sc AND document_id=:d"), {"sc": sc, "d": did}).first()
    if not meta:
        raise HTTPException(404, f"문서 없음: {sc}/{did}")
    name, total_pages = meta

    src, stage = resolve_source(name or "")
    pages_known = total_pages or db.execute(text(
        "SELECT max(end_page) FROM tb_document_chunks WHERE service_code=:sc AND document_id=:d"),
        {"sc": sc, "d": did}).scalar() or 1
    page = max(1, min(int(page), int(pages_known)))

    tri = db.execute(text("""
        SELECT route, char_count, garbage_ratio, separator_ratio, image_cover, anchor_hits, source
        FROM tb_page_triage WHERE service_code=:sc AND document_id=:d AND page_no=:p"""),
        {"sc": sc, "d": did, "p": page}).first()

    chunks = db.execute(text("""
        SELECT id, seq, heading, heading_path, content, char_count, chunk_type,
               part_index, part_total, start_page, end_page
        FROM tb_document_chunks
        WHERE service_code=:sc AND document_id=:d
          AND start_page <= :p AND coalesce(end_page, start_page) >= :p
        ORDER BY seq NULLS LAST, id"""), {"sc": sc, "d": did, "p": page}).all()

    cov = _page_coverage(db, sc, did, src, page) if src else None

    groups, scores = _dup_for_doc(db, sc, did)
    items = []
    for c in chunks:
        g = groups.get(c[0])
        peers = [k for k, v in groups.items() if v == g and k != c[0]] if g else []
        items.append({
            "id": c[0], "seq": c[1], "heading": c[2], "heading_path": c[3],
            "content": c[4], "char_count": c[5], "chunk_type": c[6] or "text",
            "part_index": c[7], "part_total": c[8], "start_page": c[9], "end_page": c[10],
            "dup_group": g, "dup_score": scores.get(g) if g else None,
            "dup_peers": peers[:8],
        })
    return templates.TemplateResponse(request, "doc.html", {
        "sc": sc, "did": did, "name": name,
        "page": page, "total_pages": int(pages_known),
        "has_source": src is not None, "stage": stage,
        "triage": tri, "chunks": items, "cov": cov,
        "dup_total": len(groups), "dup_groups": len(set(groups.values())),
    })


def _page_coverage(db: Session, sc: str, did: str, src: Path, page: int) -> dict | None:
    """이 페이지의 글자가 색인까지 갔나. 실패하면 None — 진단이 화면을 죽이면 안 된다.

    대조 대상에 **이웃 페이지(±1) 청크도 넣는다.** ODL 의 페이지 귀속이 PyMuPDF 와 한 줄씩
    어긋나는 일이 흔해서, 딱 그 페이지만 대면 페이지 끝 문장이 매번 누락으로 찍힌다(실측:
    R10 p.45 가 이 이유로 0.85 → 이웃 포함 후 실제 누락만 남았다).
    """
    try:
        rows = db.execute(text("""
            SELECT coalesce(heading_path,''), coalesce(heading,''), content
            FROM tb_document_chunks
            WHERE service_code=:sc AND document_id=:d
              AND start_page <= :p + 1 AND coalesce(end_page, start_page) >= :p - 1"""),
            {"sc": sc, "d": did, "p": page}).all()
        import pymupdf
        with pymupdf.open(src) as doc:
            if not (1 <= page <= doc.page_count):
                return None
            pdf_text = doc[page - 1].get_text("text")
        return page_report(pdf_text, [x for r in rows for x in r])
    except Exception:
        return None


@router.get("/docs/{sc}/{did}/page/{n}.png")
def page_png(sc: str, did: str, n: int, db: Session = Depends(get_db)):
    """원본 PDF 한 페이지를 PNG 로. 캐시 히트면 디스크에서 바로 준다.

    **캐시가 선택이 아닌 이유**: api 컨테이너는 mem_limit 3g 인데 BGE-M3 와 리랭커가 이미
    2GB 넘게 쓰고 있다. 페이지를 볼 때마다 렌더하면 pixmap 이 그 위에 얹혀 OOM-kill 로
    간다(실측: 이 컨테이너는 3g 에서 죽고 자동 재시작된다). 한 번 렌더하고 파일로 남긴다.
    """
    name = db.execute(text(
        "SELECT document_name FROM tb_document_extract "
        "WHERE service_code=:sc AND document_id=:d"), {"sc": sc, "d": did}).scalar()
    if not name:
        raise HTTPException(404, "문서 없음")

    out = CACHE_DIR / f"{sc}_{did}" / f"{n}@{PAGE_DPI}.png"
    if out.is_file():
        return FileResponse(out, media_type="image/png")

    src, _ = resolve_source(name)
    if src is None:
        # 원본이 이동·삭제됐을 수 있다. 화면이 통째로 깨지지 않게 이유를 분명히 준다.
        raise HTTPException(404, f"원본 PDF 없음: {name}")

    import pymupdf
    out.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(src) as doc:
        if not (1 <= n <= doc.page_count):
            raise HTTPException(404, f"페이지 범위 밖: 1~{doc.page_count}")
        pix = doc[n - 1].get_pixmap(dpi=PAGE_DPI)
        buf = io.BytesIO(pix.tobytes("png"))
    out.write_bytes(buf.getvalue())
    return FileResponse(out, media_type="image/png")
