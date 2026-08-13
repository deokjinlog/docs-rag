"""terms 적재 — extract_terms(청약철회·갱신) 결과를 product 테이블에 UPDATE.

소비자 "언제까지?"(청약철회·갱신)의 SQL 경로 데이터. extract_terms(골든 8/8)가 뽑은
cooling_off_days·is_renewable·resolution_note를 product에 채운다. **특약은 청약철회 NULL이
정답**(보통약관 준용 소관) — resolution_note로 '왜 NULL'을 남긴다(precision-first).

renewal_cycle_years·term_years는 product 컬럼이 없어 미적재(스키마 확장 후속).

--dry-run(기본): UPDATE 미리보기. --load: 실적재(psycopg2 + DATABASE_URL, localhost:5433).
용법: python3 scripts/load_terms.py --dry-run
      DATABASE_URL=... python3 scripts/load_terms.py --load
"""
import os
import sys
import importlib.util

HERE = os.path.dirname(__file__)


def _mod(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


et = _mod("extract_terms")
lp = _mod("load_payout")     # DOC_PID 단일 소스 재사용


def main():
    load = "--load" in sys.argv
    rows = []
    for doc, pid in lp.DOC_PID.items():
        t = et.extract_terms(doc)
        rows.append((pid, doc, t))
        print(f"-- {doc} ({pid}): 청약철회={t['cooling_off_days']} 갱신={t['is_renewable']}"
              f"{' · 준용 NULL' if t['cooling_off_days'] is None else ''}", file=sys.stderr)

    if not load:
        print("-- dry-run (적재하려면 --load)", file=sys.stderr)
        return

    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    for pid, _doc, t in rows:
        # is_renewable/resolution_note는 기존 값 있으면 유지(COALESCE) — extract_product 적재분 보존
        cur.execute(
            "UPDATE product SET cooling_off_days = %s, "
            "renewal_cycle_years = %s, term_years = %s, "
            "is_renewable = COALESCE(is_renewable, %s), "
            "resolution_note = COALESCE(NULLIF(resolution_note, ''), %s) "
            "WHERE product_id = %s",
            (t["cooling_off_days"], t["renewal_cycle_years"], t["term_years"],
             t["is_renewable"], t["resolution_note"], pid),
        )
    conn.commit()
    conn.close()
    print(f"적재 완료: {len(rows)}개 상품 terms UPDATE")


if __name__ == "__main__":
    main()
