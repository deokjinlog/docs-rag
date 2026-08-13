"""payout SQL 경로 골든 러너 — "얼마 받아요/언제부터 온전히?" 결정론 답변 채점.

3경로 라우터(SQL/RAG/fetch)의 SQL 경로. **질의로직은 서빙 모듈 `src/v1/rag/payout_sql.py`가
단일 소스**(extract_payout_intent·select_payout·format_payout) — 이 스크립트는 rows를 주입해
같은 로직을 채점만 한다. rows 소스 2가지:
  · 기본(in-memory): load_payout 결과 대역 — DB 없이 로직 검증.
  · --db: 실 payout_rule SELECT(psycopg2 + DATABASE_URL) — 실 DB 적재 end-to-end 검증.

용법: python3 scripts/query_payout.py              # in-memory 골든 채점
      python3 scripts/query_payout.py --db          # 실 DB 골든 채점
      python3 scripts/query_payout.py "중환자실 입원하면 하루 얼마?"   # 단건
      python3 scripts/query_payout.py --db "중환자실 하루 얼마?"       # 단건(실 DB)
"""
import os
import re
import sys
import json
import importlib.util

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_payout_qa.jsonl")

# payout SQL 로직 = 서빙 모듈 단일 소스(src/v1/rag/payout_sql.py). rag/__init__ heavy load
# 회피 + sys.path 무관하게 파일에서 직접 로드.
_ps_spec = importlib.util.spec_from_file_location(
    "payout_sql", os.path.join(HERE, "..", "src", "v1", "rag", "payout_sql.py"))
payout_sql = importlib.util.module_from_spec(_ps_spec)
_ps_spec.loader.exec_module(payout_sql)


def _mod(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


_ROWS = None


def _inmemory_rows():
    """payout_rule 대역(load_payout 결과, 전 문서). 1회 캐시."""
    global _ROWS
    if _ROWS is None:
        lp = _mod("load_payout")
        _ROWS = []
        for doc in lp.DOC_PID:
            _ROWS += lp._rows_for(doc)
    return _ROWS


# 하위호환 별칭 — assemble_answer.py(완결성·reconcile 골든)가 쓰는 API. 로직은 payout_sql 단일 소스.
_all_rows = _inmemory_rows


def _intent(q):
    return payout_sql.extract_payout_intent(q)


def answer(q):
    return payout_sql.select_payout(_inmemory_rows(), q)


def _fmt(r):
    return payout_sql.format_payout(r)


_DB_COLS = ("product_id", "coverage", "cause", "age_band", "period_bucket",
            "rate_pct", "per_unit", "limit_days",
            "reduction_rate_pct", "reduction_period", "reduction_cause", "source")


def _db_rows():
    """실 payout_rule SELECT → dict 리스트. rate_pct(Decimal)는 int로 정규화."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(f"SELECT {', '.join(_DB_COLS)} FROM payout_rule")
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("rate_pct") is not None:
                d["rate_pct"] = int(d["rate_pct"])
            rows.append(d)
        return rows
    finally:
        conn.close()


def main():
    args = [a for a in sys.argv[1:] if a != "--db"]
    use_db = "--db" in sys.argv
    rows = _db_rows() if use_db else _inmemory_rows()
    src = "실 DB payout_rule" if use_db else "in-memory 대역"

    if args:                                             # 단건 질의
        print(payout_sql.format_payout(payout_sql.select_payout(rows, args[0])))
        return

    golden = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    ok = 0
    print(f"[SQL 경로 골든 · rows={src}({len(rows)}행)]")
    print(f"{'질의':<38}{'기대':<8}{'답':<8}판정")
    print("-" * 68)
    for g in golden:
        r = payout_sql.select_payout(rows, g["query"])
        got = r.get("rate_pct") if r else None
        hit = (got == g["expect_rate"])
        ok += hit
        print(f"{g['query'][:36]:<38}{str(g['expect_rate']):<8}{str(got):<8}{'✅' if hit else '❌'}")
    print("-" * 68)
    print(f"정확도 {ok}/{len(golden)}  →  SQL 경로가 '얼마/언제'를 결정론으로 답함")
    sys.exit(0 if ok == len(golden) else 1)


if __name__ == "__main__":
    main()
