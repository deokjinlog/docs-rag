"""약관 조 파싱 품질 한눈에 — 전 문서 조 파싱이 건강한지 파악.

    uv run python scripts/check_parsing.py            # 전 문서
    uv run python scripts/check_parsing.py 중환자실    # 이름 부분매칭

각 약관을 조 단위로 파싱(stage.doc_clauses, raw에서 on-demand)해 조 수·1..N 구조·과소파싱을
점검한다. 복합약관을 첫 섹션만 파싱해 조가 확 줄면(예: KB 741p→7조) '과소파싱'으로 플래그 —
검색(청크)은 무관하나 관계형 조 파싱·parse 골든·SQL 경로가 안 열리는 신호. 순수 파일 기반(DB 불필요).
"""
import os
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


def main():
    filt = sys.argv[1] if len(sys.argv) > 1 else ""
    docs = sorted(os.path.basename(p)[:-3] for p in glob.glob(
        os.path.join(HERE, "..", "data", "output", "raw", "*.md")))
    docs = [d for d in docs if filt in d]
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
            # 판정: 구조이상 우선, 다음 과소파싱(밀도)
            if struct != "clean":
                verdict = f"⚠ 구조: {struct}"; warn += 1
            elif n and dens > DENSITY_WARN:
                verdict = f"⚠ 과소파싱 의심(복합약관?) — {n}조엔 너무 큼"; warn += 1
            else:
                verdict = "✅ 정상"
            dens_s = f"{dens//1000}K/조" if dens else "-"
            print(f"{doc[:36]:<38}{n:>5}{rng:>9}{kb:>6}KB{dens_s:>9}  {verdict}")
        except Exception as e:
            print(f"{doc[:36]:<38}  ERROR: {str(e)[:40]}")
    print("-" * 86)
    print(f"  {len(docs)}문서 · 경고 {warn}건" + (
        f"  {DIM}(과소파싱=복합약관 다절 파서 미대응, roadmap 부록 참조){RST}" if warn else ""))
    print(f"\n{DIM}조 제목·항호목까지 보려면: make check(파싱 골든) · scripts/parse_clauses.py <조회>{RST}\n")


if __name__ == "__main__":
    main()
