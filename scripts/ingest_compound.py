"""복합문서 적재 — 보통약관 + 개별 특약을 각각 미니상품으로 (parent 연결).

New치아·다이렉트처럼 제1절 보통약관 + 제2절~ 특별약관 수십 개인 복합 약관을 split_sections로
분해해, 보통약관은 부모 product(base_id), 각 특약은 자식 product(base_id_TNN,
parent_policy_id=base_id)로 적재한다. 별표는 문서(부모) 소유라 특약의 별표 참조는 부모로
리맵(annex_pid). 조 없는 stub 특약(예: 한 줄짜리)은 스킵. 벡터 색인은 별도
(index_insurance.py가 product 테이블 전체를 읽음).

용법: python3 scripts/ingest_compound.py <md경로> <base_id> [source_doc] | \
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


pc = _mod("parse_clauses")
ep = _mod("extract_product")
lc = _mod("load_clauses")
le = _mod("load_exclusions")
la = _mod("load_annexes")


def main():
    md_path, base = sys.argv[1], sys.argv[2]
    source_doc = sys.argv[3] if len(sys.argv) > 3 else pathlib.Path(md_path).name
    md = open(md_path, encoding="utf-8").read()

    secs = pc.split_sections(md)
    print("BEGIN;")
    tnum = 0
    for s in secs:
        region = s["region"]
        if not pc.parse_clauses(md, "probe", region=region):   # 조 없는 stub 특약 스킵
            continue
        if not s["parent"]:                                    # 보통약관 = 부모
            pid, parent, name = base, None, None
        else:                                                  # 개별 특약 = 자식
            tnum += 1
            pid, parent, name = f"{base}_T{tnum:02d}", base, s["name"]

        p = ep.extract_product(md, pid, source_doc, region=region,
                               parent_id=parent, name=name, annex_pid=base)
        print(f"-- {pid}: {p['product_name']}")
        print(ep.to_insert(p))
        print(lc.build_sql(md, pid, region=region, annex_pid=base))
        print(le.build_sql(md, pid, region=region))
        if not s["parent"]:                                    # 별표는 부모(보통약관)에만 1회
            print(la.build_sql(md, base))
    print("COMMIT;")


if __name__ == "__main__":
    main()
