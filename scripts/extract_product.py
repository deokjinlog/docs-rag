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


# ── 상품명 선택 ────────────────────────────────────────────────────────────
# 예전엔 "특약|보험 이 든 첫 헤딩"을 썼는데, 조 제목("제5조(보험금의 지급한도)")·안내문
# ("보험약관이란?")·판번호 조각("(2501.5)…")·문장이 먼저 걸려 8개 문서가 오염됐다.
# 상품명은 서빙에서 브랜드 해소(resolve_base_product_id)의 키라 틀리면 질의가 상품을 못 짚는다.
# → ①명백한 비-상품명을 먼저 거부하고 ②남은 후보를 **파일명과의 겹침**으로 채점해 고른다.
#   파일명이 이미 상품 정체를 담고 있어(삼성다이렉트_실손의료비보험_2605) 첫 매치보다 안정적.
_NM_JO      = re.compile(r'^(?:제\s*\d+\s*조|\d{1,2}\.\s*\()')          # 조 제목
_NM_SEC     = re.compile(r'^제\s*\d+\s*[절장관편]')                      # 절/장 표제
_NM_ONLY    = re.compile(r'^[\s약관보통목차]+$')                         # "약 관" · "보 통 약 관"
_NM_BADLEAD = re.compile(r'^(?:\d+\s*[.)\s]|[가-힣]\s*[.)]|[^\w가-힣(\[])')  # "32." · "가." · "➐"
_NM_SENT    = re.compile(r'(합니다|됩니다|입니다|습니다|하십시오)')        # 문장
_NM_LEAD    = re.compile(r'^(?:약\s*관|보\s*통\s*약\s*관)\s*[:\-–]?\s*')  # 선행 "약 관" 잡음
# 이름다운 합성어. 보험료·보험금·보험사 같은 '용어'는 뒤 글자로 배제.
_NM_CORE    = re.compile(r'[가-힣A-Za-z0-9]{2,}(?:보험|특약)(?![료금사업자])')
_NM_GUIDE   = ("약관이란", "개요", "유의사항", "제한사항", "목차", "목 차", "안내",
               "요약", "해설", "가이드", "체크", "구비서류", "이란?", "유형")
_NM_GENERIC = {"보장성보험", "저축성보험", "유병자보험", "실손보험", "종합보험",
               "손해보험", "생명보험", "미경과보험료"}


def _nm_ok(t: str) -> bool:
    if not (4 <= len(t) <= 60):               return False
    if _NM_JO.match(t) or _NM_SEC.match(t):   return False
    if _NM_ONLY.match(t):                     return False
    if any(g in t for g in _NM_GUIDE):        return False
    if _NM_SENT.search(t) or t.endswith("?"): return False
    if not re.search(r'[가-힣]{2,}', t):       return False
    if _NM_BADLEAD.match(t):                  return False
    if t in _NM_GENERIC:                      return False
    return bool(_NM_CORE.search(t))


def _nm_clean(t: str) -> str:
    return re.sub(r'\s+', ' ', _NM_LEAD.sub("", t.strip()))


def _nm_bigrams(s: str) -> set:
    s = re.sub(r'[^가-힣A-Za-z0-9]', '', s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def pick_product_name(md: str, source_doc: str) -> str | None:
    """헤딩 후보를 채점해 상품명 선택. 확신 없으면 None(precision-first — 조 제목을 상품명으로
    올리는 것보다 NULL이 낫다). 점수: 파일명 겹침×2 + 무배당 3 + H1 3."""
    ref = _nm_bigrams(re.sub(r'_?약관$', '', source_doc))
    h1m = re.search(r'^#\s+(.+?)$', md, re.MULTILINE)
    h1 = _nm_clean(h1m.group(1)) if h1m else None
    best, best_score = None, -1
    for m in re.finditer(r'^#{1,6}\s*(.+?)$', md, re.MULTILINE):
        t = _nm_clean(m.group(1))
        if not _nm_ok(t):
            continue
        sc = len(_nm_bigrams(t) & ref) * 2 + (3 if "무배당" in t else 0) + (3 if t == h1 else 0)
        if sc > best_score:
            best, best_score = t, sc
    # 점수 3 미만 = 파일명과 무관 + 무배당·H1 신호도 없음 → 상품명으로 안 본다.
    # 실측: DB다이렉트에서 '9 장애인전용보험전환 특별약관'(점수 2)이 최고점이라 본문을 못 봤다.
    if best is not None and best_score >= 3:
        return best
    # 헤딩에 상품명이 없는 문서가 있다(DB다이렉트: '다이렉트참좋은종합보험2301(CM)'이 본문에만
    # 189회·헤딩 0회). 마지막 수단으로 짧은 본문 줄을 보되 파일명과 강하게 겹칠 때만 채택.
    for line in md.splitlines():
        t = _nm_clean(line.lstrip("-*• ").strip())
        if _nm_ok(t) and len(_nm_bigrams(t) & ref) >= 3:
            return t
    return best if best_score >= 3 else None


def extract_product(md: str, product_id: str, source_doc: str,
                    region: tuple | None = None, parent_id: str | None = None,
                    name: str | None = None, annex_pid: str | None = None) -> dict:
    """region 주면 그 구간만 파싱(복합문서 특약 서브약관). parent_id/name 주면 특약으로
    처리(상품명·별표참조는 인자·부모 소유). annex_pid: 별표 참조 소유자(특약이면 부모)."""
    clauses = pc.parse_clauses(md, product_id, region=region)
    is_sub = parent_id is not None

    # 상품명: 특약이면 인자로 받음, 아니면 후보 채점으로 선택
    if name is None:
        name = pick_product_name(md, source_doc)

    # 회사 (긴 약칭 먼저 매칭)
    company = None
    for k in sorted(COMPANY_MAP, key=len, reverse=True):
        if k in md:
            company = COMPANY_MAP[k]
            break

    contract_type = "특약" if (is_sub or (name and "특약" in name)) else "주계약"
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
            payout_ref = f"{annex_pid or product_id}_별표{ma.group(1)}"  # 특약이면 별표는 부모 소유
        mc = re.search(r'회사는\s*(.+?)(?:때에는|경우에는)', b)
        if mc:
            payout_cond = re.sub(r'\s+', ' ', mc.group(1).strip())[:150]

    # 준용 → resolution_note (없는 필드가 '주계약/보통약관 소관'임을 명시)
    resolution = None
    junyong = [c for c in clauses if "준용" in c["title"]]
    if is_sub:
        resolution = (f"이 특약은 준용규정으로 보통약관({parent_id})을 따름 — 미기재 필드"
                      f"(청약철회·면책기간 등)는 보통약관 소관.")
    elif junyong:
        jo_label = junyong[0]["clause_id"].split("_")[-1]
        resolution = (f"청약철회·대기기간 등 미기재 필드는 주계약 준용({jo_label}) 소관. "
                      f"주계약 문서 미확보 → 해당 질의는 '답변 불가'가 정답.")

    return {
        "product_id": product_id, "company": company, "product_name": name,
        "contract_type": contract_type, "is_renewable": is_renewable,
        "coverage_name": cov_name, "payout_condition": payout_cond,
        "payout_table_ref": payout_ref,
        "waiting_period_days": None, "cooling_off_days": None,
        "parent_policy_id": parent_id, "resolution_note": resolution,
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
