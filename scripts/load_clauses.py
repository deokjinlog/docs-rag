"""조·참조 적재 (step 3) — 파서 산출 clause/ref를 RDB에 적재.

clause 테이블(조 단위, RAG 부모 회수용) + clause_ref(참조 그래프). 참조를 인덱싱
시점에 해소해 넣으므로 런타임 홉이 사라진다. 준용(주계약 약관)처럼 대상이 코퍼스에
없으면 resolved=false로 남겨 '코퍼스 갭 리포트'가 되게 한다.

용법: python scripts/load_clauses.py <md경로> <product_id>   → DELETE+INSERT SQL 출력
"""
import re
import sys
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "parse_clauses", pathlib.Path(__file__).parent / "parse_clauses.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def _q(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _body(v):
    return f"$clause${v}$clause$" if v else "NULL"


def build_sql(md: str, product_id: str) -> str:
    clauses = pc.parse_clauses(md, product_id)
    ids = {c["clause_id"] for c in clauses}
    lines = []

    # 멱등: 이 상품의 기존 clause_ref → clause 삭제 후 재적재
    lines.append(f"DELETE FROM clause_ref WHERE src_clause LIKE '{product_id}\\_%';")
    lines.append(f"DELETE FROM coverage_exclusion_map WHERE product_id='{product_id}';")
    lines.append(f"DELETE FROM clause WHERE product_id='{product_id}';")

    for c in clauses:
        lines.append(
            "INSERT INTO clause (clause_id, product_id, jo, hang, title, parent_id, body) "
            f"VALUES ({_q(c['clause_id'])}, {_q(product_id)}, {c['jo']}, {_q(c['hang'])}, "
            f"{_q(c['title'])}, {_q(c['parent_id'])}, {_body(c['text'])});"
        )

    for c in clauses:
        for r in pc.extract_refs(c["text"], c["jo"], product_id):
            if r["type"] == "조항":
                resolved = r["target"] in ids            # 내부 조 → 존재
            elif r["type"] == "별표":
                resolved = True                           # 별표는 문서 내(step6 적재)
            else:
                resolved = True
            lines.append(
                "INSERT INTO clause_ref (src_clause, ref_type, target, resolved) "
                f"VALUES ({_q(c['clause_id'])}, {_q(r['type'])}, {_q(r['target'])}, {_q(resolved)});"
            )
        # 준용 = 주계약 약관(외부문서) 참조 → 코퍼스에 없으면 resolved=false (갭)
        if "준용" in c["title"] or "주계약 약관을 따" in c["text"]:
            lines.append(
                "INSERT INTO clause_ref (src_clause, ref_type, target, resolved) "
                f"VALUES ({_q(c['clause_id'])}, '준용', '주계약 약관(외부문서·미확보)', false);"
            )
    return "\n".join(lines)


def main():
    md_path = sys.argv[1]
    product_id = sys.argv[2]
    md = open(md_path, encoding="utf-8").read()
    print(build_sql(md, product_id))


if __name__ == "__main__":
    main()
