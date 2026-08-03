"""payout_rule 적재기 — 검증된 지급규칙을 SQL 경로로.

extract_payout(룰베 프로파일 A/B) + extract_payout_llm(불규칙 표 LLM 폴백)의 결과를
payout_rule 테이블에 INSERT. LLM 출처 행은 source='llm'로 표시 —
정밀도 게이트(≥0.9) 통과분만 신뢰하는 precision-first 규율을 컬럼에 남긴다.

--dry-run : DB 없이 생성 INSERT SQL만 출력(오프라인 검증). 기본값.
--load    : 실제 적재(psycopg2 + DATABASE_URL). DB 스택 필요.

용법: python3 scripts/load_payout.py --dry-run
"""
import os
import sys
import glob
import importlib.util

HERE = os.path.dirname(__file__)


def _mod(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


ep = _mod("extract_payout")
epl = _mod("extract_payout_llm")

# 문서 → product_id (validate_extraction.py DOCS와 일치)
DOC_PID = {
    "라이나_중환자실입원특약": "LINA_ICU_2024",
    "라이나_소득보장수술특약": "LINA_INCOME_2024",
    "New치아보험_약관": "NEWTOOTH_2024",
    "다이렉트늘안심입원비보험_약관": "DIRECT_INPT_2024",
}
# 룰베로 커버되는 문서 / LLM 폴백이 필요한 문서(불규칙 표)
RULE_DOCS = ["라이나_중환자실입원특약", "라이나_소득보장수술특약", "New치아보험_약관"]
LLM_DOCS = ["다이렉트늘안심입원비보험_약관"]

COLS = ["product_id", "coverage", "cause", "age_band", "period_bucket", "rate_pct",
        "per_unit", "limit_days", "reduction_rate_pct", "reduction_period",
        "reduction_cause", "source", "evidence"]


def _rows_for(doc: str):
    """문서의 payout_rule 행(dict) 목록 — 룰베 or LLM 출처 태깅."""
    pid = DOC_PID[doc]
    out = []
    if doc in RULE_DOCS:
        for r in ep.extract_payout(doc):
            out.append({**r, "product_id": pid, "source": "rule",
                        "age_band": None, "evidence": None})
    if doc in LLM_DOCS:
        for r in epl.llm_extract(doc):
            out.append({"product_id": pid, "coverage": r.get("coverage"),
                        "cause": None, "age_band": r.get("age"),
                        "period_bucket": r.get("period_bucket"), "rate_pct": r.get("rate_pct"),
                        "per_unit": None, "limit_days": None, "reduction_rate_pct": None,
                        "reduction_period": None, "reduction_cause": None,
                        "source": "llm", "evidence": None})
    return out


def _sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _insert(row: dict) -> str:
    vals = ", ".join(_sql_val(row.get(c)) for c in COLS)
    return f"INSERT INTO payout_rule ({', '.join(COLS)}) VALUES ({vals});"


def main():
    load = "--load" in sys.argv
    all_rows = []
    for doc in DOC_PID:
        rows = _rows_for(doc)
        all_rows += rows
        print(f"-- {doc} ({DOC_PID[doc]}): {len(rows)}행 "
              f"[{sum(r['source']=='rule' for r in rows)} rule / {sum(r['source']=='llm' for r in rows)} llm]",
              file=sys.stderr)

    stmts = [_insert(r) for r in all_rows]
    if not load:
        print("BEGIN;")
        for s in stmts:
            print(s)
        print("COMMIT;")
        print(f"\n-- 총 {len(stmts)}행 (dry-run — 적재하려면 --load)", file=sys.stderr)
        return

    import psycopg2                                     # DB 적재 경로(스택 필요)
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM payout_rule")           # 재적재(멱등)
        for s in stmts:
            cur.execute(s)
    print(f"적재 완료: {len(stmts)}행", file=sys.stderr)


if __name__ == "__main__":
    main()
