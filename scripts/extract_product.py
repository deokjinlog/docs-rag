"""상품 메타 추출 (step 2) — 약관 md에서 product 레코드 추출 → RDB.

값이 정해진 사실(담보명·지급조건·별표참조·갱신여부)을 인덱싱 시점에 한 번 뽑아
product 테이블에 넣는다(SQL 경로). 없는 필드는 NULL로 두되 resolution_note에
'왜 NULL인지'(주계약 준용·미확보 등)를 명시 — "모름"과 "주계약 소관"을 구분.

용법: python scripts/extract_product.py <md경로> <product_id> [source_doc]
"""
import re
import sys
import json
import importlib.util
import pathlib

# 조/항 파서 재사용
_spec = importlib.util.spec_from_file_location(
    "parse_clauses", pathlib.Path(__file__).parent / "parse_clauses.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

# 약칭 → 정식 회사명 (도메인 사전; 필요 시 확장)
COMPANY_MAP = {
    "라이나생명": "라이나생명", "라이나": "라이나생명",
    "삼성생명": "삼성생명", "한화생명": "한화생명", "교보생명": "교보생명",
    "KB손해보험": "KB손해보험", "KB손보": "KB손해보험", "KB": "KB손해보험",
    "현대해상": "현대해상", "DB손해보험": "DB손해보험", "메리츠": "메리츠화재",
    "AXA손해보험": "AXA손해보험", "삼성화재": "삼성화재", "흥국화재": "흥국화재",
}


def extract_product(md: str, product_id: str, source_doc: str) -> dict:
    clauses = pc.parse_clauses(md, product_id)

    # 상품명: 제목 헤딩 (특약/보험 포함, 목차·안내 제외). 헤딩 깊이는 회사마다 다름(#####까지)
    name = None
    for m in re.finditer(r'^#{1,6}\s*(.+?(?:특약|보험)[^\n#]*)$', md, re.MULTILINE):
        t = m.group(1).strip()
        if not any(x in t for x in ("목차", "안내", "요약", "유의사항", "해설")):
            name = t
            break

    # 회사 (긴 약칭 먼저 매칭)
    company = None
    for k in sorted(COMPANY_MAP, key=len, reverse=True):
        if k in md:
            company = COMPANY_MAP[k]
            break

    contract_type = "특약" if (name and "특약" in name) else "주계약"
    is_renewable = "갱신형" in md or "특약의 갱신" in md

    # 담보명·지급조건·별표참조: '지급사유' 조에서 (조 번호는 회사마다 다름 — 라이나 제5조,
    # New치아·다이렉트 보통약관 제3조 → 번호 하드코딩 대신 제목으로 탐지)
    cov_name = payout_cond = payout_ref = None
    c5 = next((c for c in clauses if "지급사유" in c["title"]), None)
    if c5:
        b = c5["text"]
        m = re.search(r'보험수익자에게\s*([^(（]+?)\s*[(（]\s*별표', b)
        if m:
            cov_name = m.group(1).strip()
        ma = re.search(r'별표\s*(\d+)', b)
        if ma:
            payout_ref = f"{product_id}_별표{ma.group(1)}"
        mc = re.search(r'회사는\s*(.+?)(?:때에는|경우에는)', b)
        if mc:
            payout_cond = re.sub(r'\s+', ' ', mc.group(1).strip())[:150]

    # 준용 → resolution_note (없는 필드가 '주계약 소관'임을 명시)
    resolution = None
    junyong = [c for c in clauses if "준용" in c["title"]]
    if junyong:
        jo_label = junyong[0]["clause_id"].split("_")[-1]
        resolution = (f"청약철회·대기기간 등 미기재 필드는 주계약 준용({jo_label}) 소관. "
                      f"주계약 문서 미확보 → 해당 질의는 '답변 불가'가 정답.")

    return {
        "product_id": product_id, "company": company, "product_name": name,
        "contract_type": contract_type, "is_renewable": is_renewable,
        "coverage_name": cov_name, "payout_condition": payout_cond,
        "payout_table_ref": payout_ref,
        "waiting_period_days": None, "cooling_off_days": None,
        "parent_policy_id": None, "resolution_note": resolution,
        "source_doc": source_doc,
    }


def _sql(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def to_insert(p: dict) -> str:
    cols = ["product_id", "company", "product_name", "contract_type", "is_renewable",
            "coverage_name", "payout_condition", "payout_table_ref",
            "waiting_period_days", "cooling_off_days", "parent_policy_id",
            "resolution_note", "source_doc"]
    vals = ", ".join(_sql(p[c]) for c in cols)
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "product_id")
    return (f"INSERT INTO product ({', '.join(cols)}) VALUES ({vals})\n"
            f"ON CONFLICT (product_id) DO UPDATE SET {updates};")


def main():
    md_path = sys.argv[1]
    product_id = sys.argv[2]
    source_doc = sys.argv[3] if len(sys.argv) > 3 else pathlib.Path(md_path).name
    md = open(md_path, encoding="utf-8").read()
    p = extract_product(md, product_id, source_doc)

    print("=== 추출된 product ===")
    print(json.dumps(p, ensure_ascii=False, indent=2))
    print("\n=== INSERT SQL ===")
    print(to_insert(p))


if __name__ == "__main__":
    main()
