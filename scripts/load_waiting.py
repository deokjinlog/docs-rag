"""면책기간·감액 적재 — extract_waiting 결과를 KB 특약 상품에 매핑해 DB 적재.

면책기간(waiting_days) → `product.waiting_period_days`(특약 상품 컬럼).
감액(reduction) → `payout_rule` 행(product_id=특약, rate_pct=NULL[기저는 가입금액], reduction_*).

매칭: 담보명 → 특약 product_id. 특약 product_name='N. 담보명(간편가입)'이라 정규화(번호·괄호·공백
제거) 후 **정확 일치 또는 유일 부분일치만** 적재(모호하면 스킵 = precision-first, 오매칭 방지).

--dry-run(기본): 매칭·적재 미리보기. --load: 실적재(psycopg2 + DATABASE_URL, localhost:5433).
용법: python3 scripts/load_waiting.py --dry-run
      DATABASE_URL=... python3 scripts/load_waiting.py --load
"""
import os
import re
import sys
import importlib.util

HERE = os.path.dirname(__file__)


def _mod(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


ew = _mod("extract_waiting")
lt = _mod("load_terms")     # KB_BASE_PID(문서→보통약관 product_id) 재사용


def _norm(s: str) -> str:
    """매칭 키 — 번호접두·괄호·급수·<br>·공백 제거."""
    s = re.sub(r"^\s*\d+(?:-\d+)?\.\s*", "", s.replace("<br>", " "))
    s = re.sub(r"\([^)]*\)", "", s)
    return re.sub(r"\s+", "", s).strip()


def _subcontracts(cur, base_pid: str) -> dict:
    """{정규화 담보명: [product_id...]} — base의 특약들(product_name 정규화)."""
    cur.execute("SELECT product_id, product_name FROM product WHERE parent_policy_id = %s", (base_pid,))
    by_key: dict = {}
    for pid, name in cur.fetchall():
        by_key.setdefault(_norm(name or ""), []).append(pid)
    return by_key


def _match(key: str, subs: dict) -> str | None:
    """정확 일치 → 유일 부분일치만(모호=스킵, precision-first)."""
    if key in subs and len(subs[key]) == 1:
        return subs[key][0]
    hits = [pid for k, pids in subs.items() if (key in k or k in key) and len(pids) == 1 and k for pid in pids
            if len(k) >= 4]
    uniq = list({p for p in hits})
    return uniq[0] if len(uniq) == 1 else None


def main():
    load = "--load" in sys.argv
    conn = cur = None
    if load:
        import psycopg2
        conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    else:
        import psycopg2
        # dry-run도 매칭 확인차 DB 읽기 필요
        conn = psycopg2.connect(os.environ.get("DATABASE_URL",
                "postgresql://docsrag@localhost:5433/docsrag")); cur = conn.cursor()

    n_wait = n_red = n_skip = 0
    for doc, base_pid in lt.KB_BASE_PID.items():
        subs = _subcontracts(cur, base_pid)
        recs = ew.extract_waiting(doc)
        matched = skipped = 0
        for key, r in recs.items():
            pid = _match(key, subs)
            if not pid:
                skipped += 1; n_skip += 1; continue
            matched += 1
            if r["waiting_days"] is not None:
                n_wait += 1
                if load:
                    cur.execute("UPDATE product SET waiting_period_days=%s WHERE product_id=%s",
                                (r["waiting_days"], pid))
            if r["reduction_period"] is not None:
                n_red += 1
                if load:
                    ev = f"KB 감액표: {r['reduction_period']} {r['reduction_rate_pct']}%" + \
                         (f" (단 {r['sub_period_days']}일미만 {r['sub_rate_pct']}%)" if r["sub_rate_pct"] else "")
                    # 멱등: 같은 (product_id,coverage,source) 재적재 방지
                    cur.execute("DELETE FROM payout_rule WHERE product_id=%s AND coverage=%s AND source='kb_table'",
                                (pid, r["coverage"]))
                    cur.execute(
                        "INSERT INTO payout_rule (product_id, coverage, rate_pct, reduction_rate_pct, "
                        "reduction_period, source, evidence) VALUES (%s,%s,NULL,%s,%s,'kb_table',%s)",
                        (pid, r["coverage"], r["reduction_rate_pct"], r["reduction_period"], ev))
        print(f"-- {doc[:22]} ({base_pid}): 특약 {len(subs)}  담보 {len(recs)}  매칭 {matched}  스킵 {skipped}",
              file=sys.stderr)

    if load:
        conn.commit()
        print(f"적재 완료: waiting {n_wait} · reduction {n_red} · 스킵 {n_skip}")
    else:
        print(f"-- dry-run: waiting {n_wait} · reduction {n_red} · 스킵(모호/미매칭) {n_skip} (적재하려면 --load)",
              file=sys.stderr)
    conn.close()


if __name__ == "__main__":
    main()
