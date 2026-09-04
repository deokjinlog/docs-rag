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

# KB 보통약관 4종 — terms(청약철회 15일·갱신형)는 보통약관 소관이라 base 상품에만 적재.
# 757개 특약은 청약철회 NULL + resolution_note(준용 소관)를 유지(준용 NULL 철학, 여기서 안 건드림).
# payout용 DOC_PID와 분리 — KB payout 표 추출(Phase D)은 별개라 여기 넣어도 payout에 영향 없음.
# 실손 vertical(4세대 표준약관 2021.7~) 5개사 — 같은 보종이라 청약철회 15일이 표준으로 동일.
# 삼성 2건은 '보험기간은 1년만기(전기납) 1년 갱신형' 을 실제로 명시해 term_years=1(예시 아님,
# 원문 대조 확인). 현대·DB는 명시가 없어 NULL(precision-first — 표준값을 추정으로 채우지 않음).
SILSON_PID = {
    "삼성화재_실손의료비보험_2501": "SS_SILSON_2501",
    "삼성다이렉트_실손의료비보험_2605": "SSD_SILSON_2605",
    "현대해상_실손의료비보장보험_Hi1904": "HD_SILSON_1904",
    "DB손보_프로미라이프실손의료비보험_2101": "DB_PROMY_2101",
    "DB다이렉트_참좋은종합보험_2301": "DB_DIRECT_2301",
}

KB_BASE_PID = {
    "KB_골든라이프케어간편건강보험(26.01)_약관": "KB_GOLDENLIFE_2026",
    "KB_슬기로운간편실속종합건강보험(23.11)_약관": "KB_SEULGI_2023",
    "KB_플러스운전자상해보험(26.01)_약관": "KB_DRIVER_2026",
    "KB_희망플러스자녀보험II(21.07)_약관": "KB_CHILD_2021",
}


def main():
    load = "--load" in sys.argv
    rows = []
    for doc, pid in {**lp.DOC_PID, **KB_BASE_PID, **SILSON_PID}.items():
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
