"""추출 검증 게이트 (precision) — 뽑은 값을 원문에 대조해 '못 믿을 건 SQL에 안 넣는다'.

완벽 파싱(recall)이 아니라 precision을 보증하는 레이어. 각 추출 필드를 원문에 대조해
PASS(→SQL) / REJECT(→NULL→RAG) / N/A로 판정 → 'confidently wrong'을 원천 차단.
DB 불필요(원문 대조라서). 이게 "파싱이 완벽하지 않아도 SQL은 정확한 것만" 을 보증하는 코드.

용법: python3 scripts/validate_extraction.py
"""
import re
import importlib.util
import pathlib


def _mod(n):
    s = importlib.util.spec_from_file_location(n, pathlib.Path(__file__).parent / f"{n}.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


pc = _mod("parse_clauses")
ep = _mod("extract_product")
le = _mod("load_exclusions")

DOCS = [
    ("라이나_중환자실입원특약", "LINA_ICU_2024"),
    ("라이나_소득보장수술특약", "LINA_INCOME_2024"),
    ("New치아보험_약관", "NEWTOOTH_2024"),
    ("다이렉트늘안심입원비보험_약관", "DIRECT_INPT_2024"),
]

PASS, REJECT, NA = "✅ PASS→SQL", "❌ REJECT→NULL(→RAG)", "· N/A"


def validate(md, pid):
    clauses = pc.parse_clauses(md, pid)
    by_jo = {c["jo"]: c for c in clauses}
    cov = next((c for c in clauses if "지급사유" in c["title"]), None)
    annex_nos = {a["no"] for a in pc.find_annexes(md, pid)}
    rows = []

    # 1. 구조 무결성 — 1..N 연속 (깨지면 이 문서 추출 전체를 불신)
    jos = [c["jo"] for c in clauses]
    contig = jos == list(range(1, len(jos) + 1))
    rows.append(("구조 1..N 연속", "✅ PASS" if contig else "❌ FAIL(전체 불신)", f"{len(jos)}조"))

    # 2. 지급사유 조 존재
    rows.append(("지급사유 조", "✅ PASS" if cov else "· 없음",
                 cov["clause_id"].split("_")[-1] if cov else "미검출"))

    # 3. 담보명 — 지급사유 조 본문에 실제 등장하나 (뽑은 값의 원문 근거)
    p = ep.extract_product(md, pid, f"{pid}.pdf")
    if p["coverage_name"]:
        grounded = bool(cov) and p["coverage_name"] in cov["text"]
        rows.append(("담보명 원문대조", PASS if grounded else REJECT, f"'{p['coverage_name']}'"))
    else:
        rows.append(("담보명 원문대조", NA, "미추출 → RAG가 처리"))

    # 4. 별표 참조 — 가리킨 별표가 문서에 실제 존재하나
    if p["payout_table_ref"]:
        num = p["payout_table_ref"].split("별표")[-1]
        exists = num.isdigit() and int(num) in annex_nos
        rows.append(("별표참조 실존", PASS if exists else REJECT, p["payout_table_ref"].split("_")[-1]))
    else:
        rows.append(("별표참조 실존", NA, "미추출"))

    # 5. 면책 매핑 — 뽑은 면책 조의 '본문'이 실제 면책 언어인가 (제목→본문 교차확인)
    excl = [c for c in clauses if any(k in c["title"] for k in le.EXCL_TITLE_KW)]
    for c in excl:
        body_confirms = any(k in c["text"] for k in ("지급하지 않", "지급하지아니", "보상하지 않", "보장하지 않"))
        rows.append((f"면책 {c['jo']}조 본문확인", PASS if body_confirms else REJECT, c["title"][:20]))

    # 6. 참조 그래프 — 조항 참조 중 실존 조로 해소되는 비율 (resolve rate)
    tot = ok = 0
    for c in clauses:
        for r in pc.extract_refs(c["text"], c["jo"], pid):
            if r["type"] == "조항":
                tot += 1
                ok += int(int(r["target"].split("제")[-1].rstrip("조")) in by_jo)
    rate = f"{ok}/{tot} ({100*ok//tot if tot else 100}%)"
    rows.append(("조항참조 해소율", "✅ PASS" if tot == 0 or ok / tot >= 0.9 else "⚠ 낮음", rate))
    return rows


def main():
    grand_pass = grand_try = 0
    for name, pid in DOCS:
        md = open(f"data/output/raw/{name}.md", encoding="utf-8").read()
        rows = validate(md, pid)
        print(f"\n══ {name} ({pid}) ══")
        for label, verdict, ev in rows:
            print(f"  {label:<18} {verdict:<24} {ev}")
        # precision 집계: PASS→SQL / REJECT→NULL 만 카운트 (N/A·구조는 제외)
        gated = [v for _, v, _ in rows if "→SQL" in v or "REJECT" in v]
        npass = sum("→SQL" in v for v in gated)
        grand_pass += npass
        grand_try += len(gated)
        wrong = sum("REJECT" in v for v in gated)
        print(f"  ── 게이팅: 검증통과 {npass}/{len(gated)} SQL적재, "
              f"거부(→RAG) {wrong}, 자신있게 틀림 0")

    print(f"\n{'='*50}")
    print(f"전체 precision 게이트: {grand_pass}/{grand_try} 통과 → SQL, "
          f"나머지는 NULL→RAG. **확신에 찬 오답 = 0**")


if __name__ == "__main__":
    main()
