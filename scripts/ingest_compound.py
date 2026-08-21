"""복합문서 적재 — 보통약관 + 개별 특약을 각각 미니상품으로 (parent 연결).

New치아·다이렉트처럼 제1절 보통약관 + 제2절~ 특별약관 수십 개인 복합 약관을 `sections_for_ingest`로
분해해, 보통약관은 부모 product(base_id), 각 특약은 자식 product(base_id_TNN,
parent_policy_id=base_id)로 적재한다. 별표는 문서(부모) 소유라 특약의 별표 참조는 부모로
리맵(annex_pid). 조 없는 stub 특약(예: 한 줄짜리)은 스킵. 벡터 색인은 별도
(index_insurance.py가 product 테이블 전체를 읽음).

**섹션 리졸버(sections_for_ingest)**: 제N절이 있으면 기존 `split_sections`(New치아·다이렉트,
회귀 0), 없으면 조-리셋 `parse_compound` 폴백(KB·회사미상: `## 특별약관`·제N장 계열). 후자는
헤딩이 '특별약관'으로 끝나는 진짜 특약만 자식으로(준용규정 본문·인용법령 리셋 배제, precision-first).

용법(dry-run이라 스택 없이 SQL 확인 → 스택 뜨면 psql로 적재):
    python3 scripts/ingest_compound.py <md경로> <base_id> [source_doc]              # SQL 출력
    python3 scripts/ingest_compound.py <md경로> <base_id> [source_doc] | \\
        docker compose exec -T postgres psql -U docsrag -d docsrag                   # 적재
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


def sections_for_ingest(md):
    """적재용 섹션 리졸버 — 기존 `split_sections`(제N절) 우선, 없으면 조-리셋 `parse_compound` 폴백.

    제N절이 있는 복합약관(New치아·다이렉트)은 기존 경로 그대로(회귀 0 by construction). 제N절이
    없는 복합약관(KB·회사미상: `## 특별약관`·제N장 계열)만 조-리셋 불변식으로 서브계약을 커버한다.
    진짜 특약(헤딩이 '특별약관'으로 끝남)만 자식으로 승격 — 헤딩이 준용규정 본문("…이 특별약관에서
    정하지…")이거나 없는 리셋(인용법령 전문·부칙)은 precision-first로 스킵(억지 특약 product 0)."""
    secs = pc.split_sections(md)
    if len(secs) >= 2:
        return secs                                        # 제N절 복합약관: 기존 split 경로
    runs = pc.detect_subcontracts(md)
    if len(runs) < 2:
        return secs                                        # 단일 약관: split_sections 그대로(1섹션)
    out = []
    for i, r in enumerate(runs):
        end = runs[i + 1]["start"] if i + 1 < len(runs) else len(md)
        if i == 0:                                         # 첫 런 = 보통약관(부모)
            out.append({"name": None, "parent": False, "region": (r["start"], end)})
        elif r.get("heading", "").endswith("특별약관"):     # 진짜 특약만(준용규정 본문 오탐 배제)
            out.append({"name": r["heading"], "parent": True, "region": (r["start"], end)})
    return out


def main():
    src_path, base = sys.argv[1], sys.argv[2]
    source_doc = sys.argv[3] if len(sys.argv) > 3 else pathlib.Path(src_path).name

    # KB 경로: .json 주면 다단 재구성 + title-driven 세그먼트(kb_parse). 그 외는 .md 그대로.
    if src_path.endswith(".json"):
        kb = _mod("kb_parse")
        md, secs = kb.ingest_sections(src_path)
    else:
        md = open(src_path, encoding="utf-8").read()
        secs = sections_for_ingest(md)
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
