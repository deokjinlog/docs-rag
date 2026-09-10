"""검색 골든의 앵커가 **색인에 실제로 있는지** 확인한다 (스택 필요, DB만).

골든을 늘릴 때 가장 조용한 실패는 *앵커 오타*다 — 검색이 정답을 가져왔는데도 문자열이
안 맞아 miss 로 찍히고, 그걸 "검색이 나쁘다"로 읽는다. 그래서 채점 **전에** DB 에서
직접 확인한다: 앵커를 품은 청크가 몇 개인지, 어느 heading 인지.

읽는 법:
    0개  → 앵커가 틀렸다(오타·재청킹으로 사라짐). 고치기 전엔 그 문항을 믿지 마라.
    1개  → 이상적. 정답 청크가 유일하게 특정된다.
    2개+ → 중복. `table_fact` 면 앵커를 좁혀야 하고, `duplicate` 면 **그게 측정 대상**이다.

용법 (api 컨테이너 안):
    python /app/scripts/check_golden_anchors.py [golden.jsonl ...]
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import unicodedata

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DEFAULT = [ROOT / "data" / "eval" / "golden_retrieval.jsonl"]


def norm(s: str) -> str:
    """eval_retrieval._norm 과 같은 규약 — 여기서 어긋나면 검증이 무의미해진다."""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[,.·“”\"'()\[\]%]", "", s)
    return s.lower()


def main() -> int:
    paths = [pathlib.Path(p) for p in sys.argv[1:]] or DEFAULT
    rows = [json.loads(l) for p in paths for l in p.open(encoding="utf-8") if l.strip()]

    from sqlalchemy import text

    from v1.config import task_session

    docs = {r.get("document_id") for r in rows if r.get("document_id")}
    with task_session() as db:
        q = ("SELECT document_id, id, coalesce(heading_path,''), content FROM tb_document_chunks"
             + (" WHERE document_id = ANY(:d)" if docs else ""))
        chunks = db.execute(text(q), {"d": list(docs)} if docs else {}).all()
    by_doc: dict[str, list] = {}
    for d, cid, hp, c in chunks:
        by_doc.setdefault(d, []).append((cid, hp, norm(c)))
    allc = [(d, cid, hp, n) for d, v in by_doc.items() for cid, hp, n in v]

    bad = 0
    for r in rows:
        for field in ("anchor", "dup_anchor"):
            a = r.get(field)
            if not a:
                continue
            na = norm(a)
            pool = ([(r["document_id"], *t) for t in by_doc.get(r["document_id"], [])]
                    if r.get("document_id") else allc)
            hits = [(d, cid, hp) for d, cid, hp, n in pool if na in n]
            scope = r.get("document_id") or "전체"
            mark = "✅" if len(hits) == 1 else ("❌" if not hits else "⚠")
            if not hits:
                bad += 1
            print(f"{mark} {len(hits):>2}개  {scope:<8}{field:<11}{r['query'][:30]:<32}{a[:36]}")
            for d, cid, hp in hits[:4]:
                print(f"        └ [{cid}] {d} {hp[-58:]}")
    print(f"\n  문항 {len(rows)} · 앵커 미검출 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
