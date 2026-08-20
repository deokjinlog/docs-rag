"""약관 조 파싱 품질 한눈에 — 전 문서 조 파싱이 건강한지 파악.

    uv run python scripts/check_parsing.py            # 전 문서 (조수·구조·밀도 표)
    uv run python scripts/check_parsing.py 중환자실    # 이름 부분매칭
    uv run python scripts/check_parsing.py 중환자실 7  # 제7조 항①→호1.→목가. 정밀 뷰

각 약관을 조 단위로 파싱(stage.doc_clauses, raw에서 on-demand)해 조 수·1..N 구조·과소파싱을
점검한다. 복합약관을 첫 섹션만 파싱해 조가 확 줄면(예: KB 741p→7조) '과소파싱'으로 플래그 —
검색(청크)은 무관하나 관계형 조 파싱·parse 골든·SQL 경로가 안 열리는 신호. 순수 파일 기반(DB 불필요).

두 번째 인자로 조 번호(예: 7, 제7조)를 주면 그 조의 항/호/목 계층을 `parse_subitems`로 펼쳐
"제7조 ①항 2호 가목" 정밀 인용의 실물을 눈으로 확인한다(재색인·DB 불필요, 조 본문서 on-demand).
"""
import os
import re
import sys
import glob
import importlib.util

HERE = os.path.dirname(__file__)
DIM, BOLD, RST = "\033[2m", "\033[1m", "\033[0m"


def _load(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m

st = _load("stage")
pc = _load("parse_clauses")

DENSITY_WARN = 30_000   # 조당 30KB 넘으면 과소파싱 의심(정상 약관은 조당 1~10KB)


def _structure(clauses):
    jos = [c["jo"] for c in clauses]
    if not jos:
        return "조 0개"
    seen = {}
    for j in jos:
        seen[j] = seen.get(j, 0) + 1
    dup = sorted(j for j, n in seen.items() if n > 1)
    gap = sorted(set(range(min(jos), max(jos) + 1)) - set(jos))
    blank = [c["jo"] for c in clauses if not (c.get("title") or "").strip()]
    parts = []
    if gap:   parts.append(f"gap{gap[:4]}")
    if dup:   parts.append(f"dup{dup[:4]}")
    if blank: parts.append(f"blank{blank[:4]}")
    return "clean" if not parts else " ".join(parts)


def _clip(s, n=76):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def render_clause_detail(doc, jo):
    """제N조를 항①→호1.→목가. 계층으로 펼쳐 출력. '제5조 3항 2호' 정밀 인용의 실물."""
    cl = st.doc_clauses(doc)
    hit = next((c for c in cl if c["jo"] == jo), None)
    if not hit:
        print(f"\n{doc}: 제{jo}조 없음 (파싱된 조: {min(c['jo'] for c in cl)}~{max(c['jo'] for c in cl)})\n")
        return
    hangs = pc.parse_subitems(hit["text"])
    nh, nho, nmok = pc.subitem_counts(hangs)
    title = (hit.get("title") or "").strip()
    print(f"\n{BOLD}{doc}  제{jo}조 {title}{RST}  {DIM}[항 {nh} · 호 {nho} · 목 {nmok}]{RST}")
    print("-" * 86)
    if not hangs:
        print(f"  {DIM}(항/호/목 마커 없음 — 단문 조){RST}")
    for h in hangs:
        if h["hang"] > 0:                                   # 항 (①②③)
            mark = pc._CIRCLED[h["hang"] - 1] if h["hang"] <= len(pc._CIRCLED) else f"({h['hang']})"
            print(f"  {BOLD}{mark}{RST} {_clip(h['text'])}")
            ind = "     "
        else:                                               # 항 없이 호가 오는 조(면책·정의형)
            ind = "  "
        for mk in h.get("moks", []):                        # 항 직속 목(드묾)
            print(f"{ind}  {mk['mok']}. {_clip(mk['text'], 70)}")
        for ho in h["hos"]:
            print(f"{ind}{BOLD}{ho['ho']}.{RST} {_clip(ho['text'], 72)}")
            for mk in ho["moks"]:
                print(f"{ind}   {mk['mok']}. {_clip(mk['text'], 68)}")
    print("-" * 86)
    print(f"{DIM}구조 정답 잠금은 parse 골든(make check) · 로직 유닛은 tests/eval/test_subitems.py{RST}\n")


def render_subcontracts(doc):
    """복합약관을 서브계약별로 분해 파싱해 보통약관 + 특약 N개를 각 조수·제목과 함께 편다.
    detect_subcontracts로 못 잡던 특약 조(제1조부터 재시작)를 parse_compound가 복원한 결과."""
    md = st.doc_md(doc)
    subs = pc.parse_compound(md, "VIEW")
    if len(subs) < 2:
        print(f"\n{doc}: 단일 약관(서브계약 1개) — 복합 분해 불필요.\n")
        return
    named = [s for s in subs if "특별약관" in s["name"]]
    total = sum(len(s["clauses"]) for s in subs)
    print(f"\n{BOLD}{doc}{RST}  {DIM}복합약관 분해 — 서브계약 {len(subs)}개(특약 헤딩 {len(named)}) · 총 {total}조{RST}")
    print("-" * 86)
    for s in subs:
        cl = s["clauses"]
        if not cl:
            continue
        tag = "보통약관" if s["is_main"] else (_clip(s["name"], 44) or "(헤딩없음)")
        rng = f"제{cl[0]['jo']}~{cl[-1]['jo']}조" if cl else "-"
        titles = " · ".join(c["title"] for c in cl[:3])
        print(f"  {BOLD}{tag}{RST}  {DIM}{len(cl)}조 {rng}{RST}  {_clip(titles, 48)}")
    print("-" * 86)
    print(f"{DIM}메인 parse_clauses는 보통약관만(단조 break). parse_compound가 조-리셋으로 특약 복원 "
          f"— 적재 배선은 복합파서 후속.{RST}\n")


def _compound_summary(md):
    """복합약관이면 '보통약관 N조 + 특약 M개' 정밀 진단, 아니면 None.
    감지기(detect_subcontracts)로 조-리셋 서브계약을 세어, 막연한 '과소파싱'을 구조 지도로 바꾼다."""
    runs = pc.detect_subcontracts(md)
    subs = [r for r in runs if "특별약관" in r.get("heading", "")]
    if len(runs) >= 2 and subs:
        main_jo = runs[0]["count"]                          # 첫 런 = 보통약관
        return (f"복합약관 — 보통약관~{main_jo}조 + 특약 {len(subs)}개 감지"
                f"(현재 보통약관만 파싱, 복합파서 후속)")
    return None


def main():
    args = sys.argv[1:]
    subs_mode = "--subs" in args                            # 복합약관 서브계약 분해 뷰
    args = [a for a in args if a != "--subs"]
    filt = args[0] if args else ""
    # 2번째 인자가 조 번호(7 / 제7조)면 그 조의 항/호/목 정밀 뷰로 분기
    jo_arg = args[1] if len(args) > 1 else ""
    m = re.search(r"\d+", jo_arg)
    docs_all = sorted(os.path.basename(p)[:-3] for p in glob.glob(
        os.path.join(HERE, "..", "data", "output", "raw", "*.md")))
    if subs_mode:                                          # 복합약관 분해 뷰
        cand = [d for d in docs_all if filt in d]
        if not cand:
            print(f"문서 없음(필터 '{filt}').")
            return
        render_subcontracts(cand[0])
        return
    if m:
        cand = [d for d in docs_all if filt in d]
        if not cand:
            print(f"문서 없음(필터 '{filt}').")
            return
        render_clause_detail(cand[0], int(m.group()))
        if len(cand) > 1:
            print(f"{DIM}(필터 '{filt}' {len(cand)}건 중 첫 문서 {cand[0]}. 좁히려면 이름 더 구체적으로){RST}\n")
        return

    docs = [d for d in docs_all if filt in d]
    if not docs:
        print("문서 없음(raw/*.md).")
        return

    print(f"\n{BOLD}약관 조 파싱 점검{RST}  {DIM}(조 단위·raw on-demand){RST}")
    print(f"{'문서':<38}{'조수':>5}{'조범위':>9}{'크기':>8}{'밀도':>9}  판정")
    print("-" * 86)
    warn = 0
    for doc in docs:
        try:
            md = st.doc_md(doc)
            cl = st.doc_clauses(doc)
            n = len(cl)
            jos = [c["jo"] for c in cl]
            rng = f"{min(jos)}~{max(jos)}" if jos else "-"
            kb = len(md) // 1024
            dens = (len(md) // n) if n else 0
            struct = _structure(cl)
            # 판정: 경고 상황(구조이상 or 과소파싱)이면 서브계약 감지로 복합약관 정밀 진단
            if struct != "clean" or (n and dens > DENSITY_WARN):
                comp = _compound_summary(md)
                if comp:
                    verdict = f"⚠ {comp}"
                elif struct != "clean":
                    verdict = f"⚠ 구조: {struct}"
                else:
                    verdict = f"⚠ 과소파싱 의심 — {n}조엔 너무 큼"
                warn += 1
            else:
                verdict = "✅ 정상"
            dens_s = f"{dens//1000}K/조" if dens else "-"
            print(f"{doc[:36]:<38}{n:>5}{rng:>9}{kb:>6}KB{dens_s:>9}  {verdict}")
        except Exception as e:
            print(f"{doc[:36]:<38}  ERROR: {str(e)[:40]}")
    print("-" * 86)
    print(f"  {len(docs)}문서 · 경고 {warn}건" + (
        f"  {DIM}(과소파싱=복합약관 다절 파서 미대응, roadmap 부록 참조){RST}" if warn else ""))
    print(f"\n{DIM}조의 항/호/목까지 펼쳐 보려면: check_parsing.py <문서> <조번호>  "
          f"(예: check_parsing.py 중환자실 7){RST}\n")


if __name__ == "__main__":
    main()
