"""전처리 품질 게이트 — processed 산출물이 gold(청킹·서빙)로 가기 전 자동 차단.

"모델이 아니라 데이터·전처리를 단계별로 테스트하라"(The ML Test Score, Google). 깨진 문서가
청크·벡터를 오염시키기 전에 상류에서 잡는다. 골든셋(정답 채점)과 별개인 '게이트'(자동 sanity).
청크 품질이 RAG 천장이라, 청킹 전에 소스가 성한지 검사하는 게 여기.

검사:
  OCR   한글 비율(깨짐)
  파싱   조 1..N 연속(도메인 불변식) · 조 개수 sanity(5~60)
  청킹준비 조 길이 분포(sweet spot 초과 = 서브분할 필요) · 빈 조
  별표   조가 부른 별표가 문서에 실존(참조 해소율)

용법: python3 scripts/gate.py
"""
import os
import re
import glob
import importlib.util

HERE = os.path.dirname(__file__)

HANGUL_MIN = 0.30           # 한글 비율 하한(약관은 한글 문서)
JO_MIN, JO_MAX = 5, 60      # 조 개수 정상 범위
CHUNK_MAX_CHARS = 1600      # BGE-M3 sweet spot 상한(초과 조는 청킹 시 서브분할)


def _load(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


st = _load("stage")
pc = _load("parse_clauses")


def gate(doc: str) -> list:
    """processed 문서 → 검사 결과 [(항목, 판정, 상세)]. FAIL 하나면 gold 진입 차단."""
    md = st.doc_md(doc)
    clauses = st.doc_clauses(doc)
    jos = [c["jo"] for c in clauses]
    rows = []

    # OCR — 한글 비율
    hangul = len(re.findall(r"[가-힣]", md))
    nonspace = len(re.sub(r"\s", "", md)) or 1
    ratio = hangul / nonspace
    rows.append(("한글 비율", "PASS" if ratio >= HANGUL_MIN else "FAIL", f"{ratio:.0%}"))

    # 복합약관은 단순 파스가 보통약관만 잡아 조-count/연속이 부적용(parse_compound 소관) → FAIL 대신
    # WARN(오차단 방지). 서브계약이 1개면 단일 약관이라 아래 엄격 검사 그대로.
    n_sub = len(pc.detect_subcontracts(md))
    if n_sub >= 2:
        rows.append(("복합약관 감지", "WARN",
                     f"서브계약 {n_sub}개 → 단순 조-count 부적용, parse_compound/ingest_compound 소관"))
    else:
        # 파싱 — 조 1..N 연속(불변식)
        contig = jos == list(range(1, len(jos) + 1))
        rows.append(("조 1..N 연속", "PASS" if contig else "FAIL",
                     f"{len(jos)}조" if contig else f"끊김: {jos[:12]}…"))
        # 파싱 — 조 개수 sanity
        rows.append(("조 개수 sanity", "PASS" if JO_MIN <= len(jos) <= JO_MAX else "FAIL",
                     f"{len(jos)} (정상 {JO_MIN}~{JO_MAX})"))

    # 청킹준비 — 빈 조
    empty = [c["jo"] for c in clauses if not c["text"].strip()]
    rows.append(("빈 조 없음", "PASS" if not empty else "WARN", f"빈 조 {empty}" if empty else "0"))

    # 청킹준비 — 조 길이 분포(sweet spot 초과 = 서브분할 필요)
    longs = [c["jo"] for c in clauses if len(c["text"]) > CHUNK_MAX_CHARS]
    mx = max((len(c["text"]) for c in clauses), default=0)
    rows.append(("조 길이 분포", "PASS" if not longs else "WARN",
                 f"최대 {mx}자, >{CHUNK_MAX_CHARS} {len(longs)}조 → 청킹 서브분할"))

    # 별표 — 참조 해소율(조가 부른 '내부' 별표가 문서에 실존; 외부 법령 별표는 제외)
    annex_nos = {a["no"] for a in pc.find_annexes(md, "X")}
    refs = set()
    for m in re.finditer(r"별\s*표\s*(\d+)", md):
        pre = md[max(0, m.start() - 20):m.start()]
        if re.search(r"[」』]|시행규칙|시행령|법\s*$|규칙", pre):   # 「의료법 시행규칙」 별표4 = 외부, 제외
            continue
        refs.add(int(m.group(1)))
    unresolved = refs - annex_nos
    rate = f"{len(refs & annex_nos)}/{len(refs)}" if refs else "참조 없음"
    rows.append(("별표 참조 해소", "PASS" if not unresolved else "WARN",
                 f"{rate}" + (f", 미해소 {sorted(unresolved)}" if unresolved else "")))
    return rows


def main():
    docs = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")))
    grand = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for doc in docs:
        rows = gate(doc)
        verdict = "FAIL" if any(v == "FAIL" for _, v, _ in rows) else \
                  "WARN" if any(v == "WARN" for _, v, _ in rows) else "PASS"
        mark = {"PASS": "✅ 통과→gold", "WARN": "⚠ 통과(주의)", "FAIL": "❌ 차단"}[verdict]
        print(f"\n══ {doc[:22]}  →  {mark}")
        for label, v, ev in rows:
            grand[v] = grand.get(v, 0) + 1
            icon = {"PASS": "✅", "WARN": "⚠", "FAIL": "❌"}[v]
            print(f"   {icon} {label:<16} {ev}")
    print(f"\n{'='*54}")
    print(f"게이트 집계: PASS {grand['PASS']} · WARN {grand['WARN']} · FAIL {grand['FAIL']}  "
          f"→ FAIL 문서는 gold(청킹·색인) 진입 차단")


if __name__ == "__main__":
    main()
