"""면책 매핑 적재 (step 4) — 담보 → 면책/감액 조항.

보장·지급 질의 시 검색 점수와 무관하게 강제 첨부할 조항을 미리 매핑한다(안전장치).
가장 위험한 실패가 "보장됩니다"라 답하고 면책을 누락하는 것이라, 규칙 기반으로
확실히 건다. 두 종류를 잡는다:
  - general  : 조 제목에 면책 키워드(지급하지 않는 등) — 제7조
  - reduction: 제목엔 없지만 본문이 실질 감액("해당 보험금의 50%를 지급") — 제6조

용법: python scripts/load_exclusions.py <md경로> <product_id>
"""
import re
import sys
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "parse_clauses", pathlib.Path(__file__).parent / "parse_clauses.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

EXCL_TITLE_KW = ["지급하지 않는", "지급하지 아니", "보상하지 않는", "면책",
                 "부담보", "보장하지 않는", "지급하지않는"]
# 본문 실질 감액: 담보(보험금/급여금)의 N% 지급 — 정의·적립이율 등의 %는 배제.
REDUCE_RE = re.compile(
    r'(?:보험금|급여금)\s*의?\s*\d+\s*%\s*(?:만\s*)?(?:을|를)?\s*지급'
    r'|감액하여?\s*지급'
)


def _q(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def build_sql(md: str, product_id: str, region: tuple | None = None) -> str:
    clauses = pc.parse_clauses(md, product_id, region=region)
    cov_clause = next((c for c in clauses if "지급사유" in c["title"]), None)
    cov_id = cov_clause["clause_id"] if cov_clause else None   # None → _q가 SQL NULL 생성
    # ('NULL' 문자열이면 coverage_clause FK 위반 — 지급사유 없는 제도성 특약에서 발생)

    lines = [f"DELETE FROM coverage_exclusion_map WHERE product_id={_q(product_id)};"]
    cov_expr = f"(SELECT coverage_name FROM product WHERE product_id={_q(product_id)})"

    # 정의·준용·목적 조는 담보 규칙이 아니라 용어정의/참조라 감액 오탐 원천 → 감액 판정 제외
    _SKIP_REDUCE = ("정의", "준용", "목적")
    for c in clauses:
        general = any(kw in c["title"] for kw in EXCL_TITLE_KW)
        reduction = (not general
                     and not any(x in c["title"] for x in _SKIP_REDUCE)
                     and bool(REDUCE_RE.search(c["text"])))
        if not (general or reduction):
            continue
        kind = "general" if general else "reduction"
        lines.append(
            "INSERT INTO coverage_exclusion_map "
            "(product_id, coverage_name, coverage_clause, exclusion_clause, kind) "
            f"VALUES ({_q(product_id)}, {cov_expr}, {_q(cov_id)}, {_q(c['clause_id'])}, {_q(kind)});"
        )
    return "\n".join(lines)


def main():
    md = open(sys.argv[1], encoding="utf-8").read()
    print(build_sql(md, sys.argv[2]))


if __name__ == "__main__":
    main()
