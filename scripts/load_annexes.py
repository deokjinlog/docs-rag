"""별표 적재 (step 6) — 별표 섹션 → annex (분류표는 annex_row까지).

별표는 검색이 아니라 fetch 경로다: 조가 ID로 부른다(제5조 → 별표1). 그래서 벡터화하지
않고 raw_markdown을 통째로 저장(payout/formula)해 답변 때 참조로 끌어온다. 분류표만
행 단위로도 쪼개(annex_row) ICD 코드 어휘검색이 되게 한다("F32 우울증 보장?" →
F00~F99 미보장 행). summary만 임베딩(안전망), 실제 값은 raw_markdown/annex_row에.

용법: python3 scripts/load_annexes.py <md경로> <product_id>
"""
import re
import sys
import json
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "parse_clauses", pathlib.Path(__file__).parent / "parse_clauses.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

# ICD-10 코드/범위: A00, F00~F99, Y35.5, S00~Y84 ...
RE_ICD = re.compile(r'[A-Z]\d{2}(?:\.\d+)?(?:\s*~\s*[A-Z]\d{2}(?:\.\d+)?)?')


def _q(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def _summary(a: dict) -> str:
    """임베딩용 한 줄 요약(안전망). 값 자체는 raw_markdown/annex_row에 있다."""
    if a["kind"] == "classification":
        codes = list(dict.fromkeys(c.replace(" ", "") for c in RE_ICD.findall(a["raw_markdown"])))
        return f"{a['title']} — 분류코드 {', '.join(codes[:6])} 등"
    for ln in a["raw_markdown"].splitlines():        # payout/formula: 제목·헤딩 반복 건너뛴 첫 실질 줄
        s = ln.strip(" #-|")
        if len(s) < 8 or "별표" in s or s in a["title"] or a["title"] in s:
            continue
        return f"{a['title']} — {s[:90]}"
    return a["title"]


def _rows(a: dict) -> list[dict]:
    """분류표만 행 단위 분해: 보장/미보장 구획 태깅 + ICD 코드 추출 → 어휘검색용."""
    if a["kind"] != "classification":
        return []
    rows, section = [], None
    for ln in a["raw_markdown"].splitlines():
        s = ln.strip()
        if "보장대상이 되는" in s:
            section = "보장"
        elif "지급하지 않는" in s or "제외하여" in s:
            section = "미보장"
        codes = [c.replace(" ", "") for c in RE_ICD.findall(s)]
        if codes:
            rows.append({"section": section, "codes": codes,
                         "text": re.sub(r'^[\s\-①-⑳•*]+', '', s)[:200]})
    return rows


def build_sql(md: str, product_id: str) -> str:
    ann = pc.find_annexes(md, product_id)
    lines = [f"DELETE FROM annex_row WHERE annex_id LIKE '{product_id}\\_별표%';",
             f"DELETE FROM annex WHERE product_id='{product_id}';"]
    for a in ann:
        lines.append(
            "INSERT INTO annex (annex_id, product_id, annex_no, title, kind, raw_markdown, summary) "
            f"VALUES ({_q(a['annex_id'])}, {_q(product_id)}, {a['no']}, {_q(a['title'])}, "
            f"{_q(a['kind'])}, $annex${a['raw_markdown']}$annex$, {_q(_summary(a))});")
        for i, r in enumerate(_rows(a), 1):
            lines.append(
                "INSERT INTO annex_row (annex_id, row_no, cols) "
                f"VALUES ({_q(a['annex_id'])}, {i}, "
                f"{_q(json.dumps(r, ensure_ascii=False))}::jsonb);")
    return "\n".join(lines)


def main():
    md = open(sys.argv[1], encoding="utf-8").read()
    print(build_sql(md, sys.argv[2]))


if __name__ == "__main__":
    main()
