"""KB 암 별표3 코드셋 → coverage_range 적재. product_id로 스코프(교차회사 오염 방지).

extract_kb_coverage.coverage_ranges_kb(범위 뺄셈으로 담보 특정성)를 coverage_range 테이블에 적재.
**product_id=base(회사 스코프)** — 다이렉트 암진단자금(C73~C75 포함)과 담보명은 다르지만, /answer
coverage 분기가 get_ranges를 브랜드 base로 스코프하므로 KB 질의는 KB 행만 본다(교차회사 리다이렉트
차단). 멱등: 같은 product의 KB coverage 행 재적재 전 삭제.

--dry-run(기본) / --load. 용법: DATABASE_URL=... python3 scripts/load_kb_coverage.py --load
"""
import os
import sys
import importlib.util

HERE = os.path.dirname(__file__)


def _mod(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


ek = _mod("extract_kb_coverage")
lt = _mod("load_terms")     # KB_BASE_PID

# 암 별표3 보유 KB 문서(운전자상해는 암 담보 없음 → 제외)
KB_CANCER_DOCS = [
    "KB_골든라이프케어간편건강보험(26.01)_약관",
    "KB_슬기로운간편실속종합건강보험(23.11)_약관",
    "KB_희망플러스자녀보험II(21.07)_약관",
]


def main():
    load = "--load" in sys.argv
    plan = []
    for doc in KB_CANCER_DOCS:
        pid = lt.KB_BASE_PID[doc]
        ranges = ek.coverage_ranges_kb(doc)
        for cov, toks in ranges.items():
            for t in toks:
                plan.append((pid, cov, t))
        print(f"-- {doc[:22]} ({pid}): {sum(len(v) for v in ranges.values())}행 "
              f"({', '.join(f'{k} {len(v)}' for k, v in ranges.items())})", file=sys.stderr)

    if not load:
        print(f"-- dry-run: coverage_range {len(plan)}행 (적재하려면 --load)", file=sys.stderr)
        return

    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    for pid in {p for p, _, _ in plan}:                    # 멱등: KB coverage 재적재 전 해당 product 삭제
        cur.execute("DELETE FROM coverage_range WHERE product_id = %s", (pid,))
    for pid, cov, tok in plan:
        cur.execute("INSERT INTO coverage_range (product_id, coverage, code_token) VALUES (%s,%s,%s)",
                    (pid, cov, tok))
    conn.commit(); conn.close()
    print(f"적재 완료: coverage_range {len(plan)}행 (KB 암 별표3)")


if __name__ == "__main__":
    main()
