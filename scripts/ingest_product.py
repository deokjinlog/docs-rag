"""상품 관계형 일괄 적재 (steps 2·3·4·6 오케스트레이터).

약관 md 하나 → product + clause + clause_ref + coverage_exclusion_map + annex(+row)
SQL을 한 번에 생성. FK 의존 순서(product → clause → exclusion → annex)로 배열하고
BEGIN/COMMIT으로 원자 적재. 벡터 색인(step5)은 BGE-M3가 필요해 별도로 컨테이너에서:
  docker compose exec api uv run python scripts/index_insurance.py <product_id>

용법: python3 scripts/ingest_product.py <md경로> <product_id> [source_doc] | \
        docker compose exec -T postgres psql -U docsrag -d docsrag
"""
import sys
import pathlib
import importlib.util


def _mod(name):
    spec = importlib.util.spec_from_file_location(
        name, pathlib.Path(__file__).parent / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ep = _mod("extract_product")
lc = _mod("load_clauses")
le = _mod("load_exclusions")
la = _mod("load_annexes")


def main():
    md_path, product_id = sys.argv[1], sys.argv[2]
    source_doc = sys.argv[3] if len(sys.argv) > 3 else pathlib.Path(md_path).name
    md = open(md_path, encoding="utf-8").read()

    print("BEGIN;")
    print("-- step2 product (고정 사실 → SQL 경로)")
    print(ep.to_insert(ep.extract_product(md, product_id, source_doc)))
    print("-- step3 clause + clause_ref (조 본문 + 참조 그래프)")
    print(lc.build_sql(md, product_id))
    print("-- step4 coverage_exclusion_map (면책·감액 강제첨부)")
    print(le.build_sql(md, product_id))
    print("-- step6 annex + annex_row (별표 fetch + 분류표 행)")
    print(la.build_sql(md, product_id))
    print("COMMIT;")


if __name__ == "__main__":
    main()
