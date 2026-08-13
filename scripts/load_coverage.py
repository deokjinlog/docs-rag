"""coverage_range 적재 — judge_coverage.coverage_ranges(별표3 ICD) → serving 테이블.

서빙(coverage_sql.judge_coverage)이 doc 파싱 없이 SELECT로 판정하게 {담보:[코드토큰]}을
coverage_range(product_id, coverage, code_token)로 적재. 담보별 코드범위 별표3이 있는 문서만
(다이렉트=암/제자리암/경계성). 라이나·New치아는 별표3 구조가 달라 coverage_ranges 빈값 → 미적재.

--dry-run(기본): 미리보기. --load: 실적재(psycopg2 + DATABASE_URL, localhost:5433).
"""
import os
import sys
import importlib.util

HERE = os.path.dirname(__file__)


def _mod(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


jc = _mod("judge_coverage")
lp = _mod("load_payout")     # DOC_PID 단일 소스


def main():
    load = "--load" in sys.argv
    rows = []  # (product_id, coverage, code_token)
    for doc, pid in lp.DOC_PID.items():
        ranges = jc.coverage_ranges(doc)
        n = 0
        for cov, toks in ranges.items():
            for tok in dict.fromkeys(toks):    # dedup, 순서 보존
                rows.append((pid, cov, tok)); n += 1
        if n:
            print(f"-- {doc} ({pid}): {len(ranges)}담보 {n}코드토큰", file=sys.stderr)

    if not load:
        print(f"-- dry-run: 총 {len(rows)}행 (적재하려면 --load)", file=sys.stderr)
        return

    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("DELETE FROM coverage_range")          # 재적재(멱등)
    cur.executemany(
        "INSERT INTO coverage_range (product_id, coverage, code_token) VALUES (%s, %s, %s)", rows)
    conn.commit()
    conn.close()
    print(f"적재 완료: {len(rows)}행")


if __name__ == "__main__":
    main()
