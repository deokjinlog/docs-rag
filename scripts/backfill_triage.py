"""기존 색인 문서에 페이지 판별을 소급 적용 (ODL 트리 파생).

신규 문서는 extract 직전에 PyMuPDF 로 정확히 재지만, 이미 색인된 문서는 그 시점이 지났다.
원본 PDF 를 다시 열 수도 있지만 **트리가 이미 DB 에 있으므로** 그걸 쓴다 — 수천 페이지의
PDF 를 다시 파싱하지 않아도 되고, 무엇보다 원본이 사라진 문서에도 적용된다.

⚠ **파생값은 신규 판정과 같지 않다.** ODL 트리에는 rotation·폰트 정보가 없고, 재는 대상도
*"PDF 에 뭐가 있나"* 가 아니라 *"ODL 이 뭘 내놨나"* 다. "텍스트 레이어가 없다"와 "ODL 이
실패했다"를 구분하지 못한다. 다만 **둘 다 SCAN 으로 가야 하는 건 같아서** 라우팅 목적에는
지장이 없다. 섞이지 않게 `source='odl_tree'` 로 표시하고, 분포를 해석할 땐 이걸 본다.

용법 (api/celery 컨테이너 안):
    python /app/scripts/backfill_triage.py --dry-run
    python /app/scripts/backfill_triage.py --apply
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--doc", help="특정 document_id 만")
    a = ap.parse_args()
    if not (a.apply or a.dry_run):
        ap.error("--apply 또는 --dry-run")

    from sqlalchemy import text

    from v1.config import task_session
    from v1.repository import PageTriageRepository
    from v1.utils.triage import classify, flatten_odl, load_config, signals_from_odl

    cfg = load_config()
    total = collections.Counter()
    per_doc: list[tuple[str, int, dict]] = []

    with task_session() as db:
        q = "SELECT service_code, document_id, raw_json FROM tb_document_extract WHERE raw_json IS NOT NULL"
        params = {}
        if a.doc:
            q += " AND document_id = :d"
            params["d"] = a.doc
        rows = db.execute(text(q + " ORDER BY document_id"), params).all()
        print(f"  대상 문서 {len(rows)}개")

        for sc, did, rj in rows:
            nodes = flatten_odl(rj)
            pages: dict[int, list] = collections.defaultdict(list)
            for n in nodes:
                if n["pg"] is not None:
                    pages[n["pg"]].append(n)
            out, dist = [], collections.Counter()
            for pg in sorted(pages):
                sig = signals_from_odl(pages[pg], pg, cfg)
                route = classify(sig, cfg)
                dist[route] += 1
                total[route] += 1
                out.append({
                    "page_no": pg, "route": route,
                    "char_count": sig.char_count,
                    "garbage_ratio": sig.garbage_ratio,
                    "separator_ratio": sig.separator_ratio,
                    "image_cover": sig.image_cover,
                    "anchor_hits": sig.anchor_hits,
                    "font_flags": None,      # 트리에 폰트 정보가 없다 — 없는 걸 추측하지 않는다
                    "source": "odl_tree",
                })
            per_doc.append((did, len(out), dict(dist)))
            if a.apply:
                PageTriageRepository(db).replace_document(sc, did, out)
        if a.apply:
            db.commit()

    for did, n, dist in per_doc:
        print(f"    {did:<16}{n:>5}p  {dist}")
    print(f"\n  합계 {sum(total.values())}p · {dict(total)}")
    if not a.apply:
        print("  (dry-run — 적재하지 않음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
