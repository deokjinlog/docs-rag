"""산출물에서 문서 테이블 복구 — Qdrant payload + 디스크 raw 로 DB 를 되살린다.

**언제 쓰나**: `db/schema.sql` 을 실행하면 맨 앞의 `DROP TABLE IF EXISTS` 8줄이 문서 계열
테이블을 통째로 지운다(파일 상단에 "실행 전 기존 테이블 백업 필수"라고 적혀 있다).
2026-09-10 에 실제로 그렇게 날렸다. 다행히 **비싼 것은 다 밖에 있었다**:

    Qdrant  8,850점 · payload 에 content·heading_path·page_range 전부 → 재임베딩 불필요
    디스크  data/output/raw/*.md·*.json 44쌍 → ODL 재추출 불필요
    보험 관계형(product·clause·payout_rule…)은 DROP 목록에 없어 무사

그래서 복구는 "다시 만드는" 게 아니라 **"흩어진 걸 도로 모으는"** 일이다.

document_id ↔ document_name 매핑은 사라졌으므로 **본문으로 되찾는다** — 각 문서의 첫 청크
content 를 raw .md 들과 대조해 어느 파일에서 나왔는지 찾는다. 정규화 후 포함 검사라
공백·개행 차이를 흡수한다.

용법 (celery/api 컨테이너 안):
    python /app/scripts/recover_from_artifacts.py --dry-run
    python /app/scripts/recover_from_artifacts.py --apply
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

QDRANT = "http://qdrant:6333"
COLLECTION = "docs_rag_v1"
RAW = ROOT / "data" / "output" / "raw"

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub("", s or "")


def _pg_safe(s: str | None) -> str | None:
    """Postgres 는 텍스트·JSONB 에 NUL 을 못 담는다(UntranslatableCharacter).

    ODL 출력에 `\u0000` 이스케이프가 섞여 있어서 원본 그대로 넣으면 적재가 통째로 실패한다.
    이스케이프 표기와 실제 NUL 을 둘 다 지운다 — 내용상 의미 없는 문자다.
    """
    if s is None:
        return None
    return s.replace("\\u0000", "").replace("\x00", "")


def scroll_all() -> list[dict]:
    pts, off = [], None
    while True:
        body = {"limit": 1000, "with_payload": True, "with_vector": False}
        if off is not None:
            body["offset"] = off
        req = urllib.request.Request(f"{QDRANT}/collections/{COLLECTION}/points/scroll",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)["result"]
        pts += res["points"]
        off = res.get("next_page_offset")
        if not off:
            break
    return pts


# 이 세션에서 등록한 문서들 — 인벤토리(2026-09-09 생성) 이후라 거기 없다.
# 하드코딩이 싫지만 로그에만 남은 매핑이라 여기 적어 두는 게 정직하다.
SESSION_DOCS = {
    "B005":   "37204_봉천제14구역 주택재개발정비사업 환경영향평가서 주민 등의 의견수렴 및 반영결과",
    "D14101": "40969_봉천제413구역 수용재결신청서 열람공고",
    "D14102": "41116_고시문(신림5구역 주택정비형 재개발사업 사업시행자 지정 고시)",
    "D14103": "39904_공시송달 공고 목록(신림2)",
}
INVENTORY = ROOT / "data" / "bench" / "source_inventory.json"


def match_documents(by_doc: dict[str, list[dict]]) -> dict[str, pathlib.Path]:
    """document_id → raw .md 경로.

    순서가 중요하다:
      ① **인벤토리**(드롭 전에 만들어 둔 문서 목록) — 권위 있는 매핑
      ② 이 세션 등록분 하드코딩
      ③ 본문 대조 — 마지막 수단이다. 유사한 약관끼리 같은 문장을 공유해서 오매칭이 난다
         (실측: R05·R06 이 같은 md 로, R07·R08 도 같은 md 로 잡혔다)
    """
    mds = {p: _norm(p.read_text(encoding="utf-8", errors="replace")) for p in RAW.glob("*.md")}
    by_stem = {p.stem: p for p in mds}
    out: dict[str, pathlib.Path] = {}

    if INVENTORY.is_file():
        try:
            inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
            for r in inv.get("indexed_documents", []):
                stem = pathlib.Path(r.get("document_name", "")).stem
                if r.get("document_id") in by_doc and stem in by_stem:
                    out[r["document_id"]] = by_stem[stem]
        except Exception as e:
            print(f"  ⚠ 인벤토리 읽기 실패({e}) — 본문 대조로만 진행", file=sys.stderr)
    for did, stem in SESSION_DOCS.items():
        if did in by_doc and stem in by_stem:
            out.setdefault(did, by_stem[stem])

    for did, pts in by_doc.items():
        if did in out:
            continue
        # 짧은 청크는 여러 문서에 우연히 들어맞을 수 있다 → 긴 것부터 시도
        cands = sorted(pts, key=lambda p: -len(p["payload"].get("content") or ""))[:5]
        hits: collections.Counter = collections.Counter()
        for p in cands:
            probe = _norm(p["payload"].get("content") or "")[:200]
            if len(probe) < 40:
                continue
            for path, body in mds.items():
                if probe in body:
                    hits[path] += 1
        if hits:
            top, n = hits.most_common(1)[0]
            if n >= 1:
                out[did] = top
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (a.apply or a.dry_run):
        ap.error("--apply 또는 --dry-run")

    pts = scroll_all()
    by_doc: dict[str, list[dict]] = collections.defaultdict(list)
    for p in pts:
        by_doc[p["payload"].get("document_id")].append(p)
    print(f"  Qdrant {len(pts)}점 · 문서 {len(by_doc)}개")

    mapping = match_documents(by_doc)
    print(f"  본문 대조로 원본 md 찾음: {len(mapping)}/{len(by_doc)}")
    for did in sorted(by_doc):
        mark = "✓" if did in mapping else "✗"
        name = mapping[did].name if did in mapping else "— (raw md 미발견)"
        print(f"    {mark} {did:<14}{len(by_doc[did]):>5}청크  {name[:52]}")

    if a.dry_run:
        return 0

    from sqlalchemy import text
    from v1.config import task_session
    from v1.utils.source import resolve_source, sha256_of

    ins_chunk = text("""
        INSERT INTO tb_document_chunks
          (id, service_code, document_id, seq, heading, heading_path, content, char_count,
           start_page, end_page, chunk_type, chunk_strategy, part_index, part_total,
           image_paths, image_ocr_texts)
        VALUES (:id,:sc,:did,:seq,:h,:hp,:c,:cc,:sp,:ep,:ct,:cs,:pi,:pt,
                CAST(:ip AS JSONB), CAST(:iot AS JSONB))
        ON CONFLICT (id) DO NOTHING""")
    ins_cont = text("""
        INSERT INTO tb_document_contents
          (service_code, document_id, chunk_id, heading, heading_path, content,
           start_page, end_page, chunk_type, chunk_strategy, part_index, part_total,
           image_paths, image_ocr_texts, qdrant_point_id, char_count)
        VALUES (:sc,:did,:id,:h,:hp,:c,:sp,:ep,:ct,:cs,:pi,:pt,
                CAST(:ip AS JSONB), CAST(:iot AS JSONB), :id, :cc)""")

    n_chunk = n_cont = n_ext = n_st = 0
    with task_session() as db:
        for did, plist in by_doc.items():
            sc = plist[0]["payload"].get("service_code") or "01"
            for p in plist:
                pl = p["payload"]
                pr = pl.get("page_range") or [None, None]
                row = dict(id=int(p["id"]), sc=sc, did=did, seq=pl.get("seq"),
                           h=pl.get("heading"), hp=pl.get("heading_path"),
                           c=_pg_safe(pl.get("content")) or "", cc=len(pl.get("content") or ""),
                           sp=pr[0] if pr else None, ep=pr[1] if len(pr) > 1 else None,
                           ct=pl.get("chunk_type") or "text", cs=pl.get("chunk_strategy"),
                           pi=pl.get("part_index"), pt=pl.get("part_total"),
                           ip=json.dumps(pl.get("image_paths")) if pl.get("image_paths") else None,
                           iot=json.dumps(pl.get("image_ocr_texts")) if pl.get("image_ocr_texts") else None)
                db.execute(ins_chunk, row); n_chunk += 1
                db.execute(ins_cont, row); n_cont += 1

            md = mapping.get(did)
            if md:
                name = md.stem + ".pdf"
                js = md.with_suffix(".json")
                raw_json = js.read_text(encoding="utf-8", errors="replace") if js.is_file() else None
                src, stage = resolve_source(name)
                db.execute(text("""
                    INSERT INTO tb_document_extract
                      (service_code, document_id, document_name, document_path, total_pages,
                       raw_json, raw_markdown, sha256, source_path_resolved, source_stage)
                    VALUES (:sc,:did,:name,:path,:tp, CAST(:rj AS JSONB), :rm,:sha,:res,:stage)"""),
                    dict(sc=sc, did=did, name=name,
                         path=f"/app/data/input/{name}",
                         tp=(json.loads(raw_json).get("number of pages") if raw_json else None),
                         rj=_pg_safe(raw_json), rm=_pg_safe(md.read_text(encoding="utf-8", errors="replace")),
                         sha=sha256_of(src) if src else None,
                         res=str(src) if src else None, stage=stage))
                n_ext += 1
                db.execute(text("""
                    INSERT INTO tb_document_status
                      (service_code, document_id, document_name, document_path, status_code)
                    VALUES (:sc,:did,:name,:path,'11')
                    ON CONFLICT (service_code, document_id) DO UPDATE SET status_code='11'"""),
                    dict(sc=sc, did=did, name=name, path=f"/app/data/input/{name}"))
                n_st += 1
        db.commit()
    # BIGSERIAL 시퀀스를 복원한 최대 id 뒤로 밀어둔다 — 안 하면 다음 insert 가 PK 충돌한다
    with task_session() as db:
        db.execute(text("SELECT setval(pg_get_serial_sequence('tb_document_chunks','id'), "
                        "COALESCE((SELECT MAX(id) FROM tb_document_chunks),1))"))
        db.commit()
    print(f"\n  복구: chunks {n_chunk} · contents {n_cont} · extract {n_ext} · status {n_st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
