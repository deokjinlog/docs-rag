"""KB 계열 복합약관 특약 추출 — 다단 재구성 + title-driven 세그먼테이션 (LLM 불필요).

KB 약관(700~1200p·2단·이미지-heavy)은 ODL이 단을 가로질러 읽어 조가 뒤섞여, 조-리셋 복합파서
(`parse_compound`)로는 특약 제목을 못 붙였다(roadmap 복합파서 절 참조). 열쇠는 **읽기순서 재구성**
(`reconstruct_reading_order`)이 특약 제목(`###### N. 담보명`)을 그 특약의 제1조 바로 앞에 놓는 것.

파이프라인: `.json` → 재구성 → **title 헤딩으로 세그먼트**(각 `###### N. 담보명` → 다음 title까지가
그 특약) → 준용규정 조 보유 판별식으로 요약/용어/조건 배제. 실측(4개 KB): 골든라이프 69·슬기로운
89·운전자 201·자녀 374 특약을 실 담보명으로 추출(중복 없음). 적재는 후속(ingest_compound 연결 + docker).

용법:  uv run python scripts/kb_parse.py "<KB문서명>"   # 추출된 특약 목록(이름·조수) 출력
"""
import os
import re
import sys
import importlib.util

HERE = os.path.dirname(__file__)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rr = _mod("reconstruct_reading_order")
pc = _mod("parse_clauses")

# 담보 아닌 헤딩(용어·조건·제도 조항)을 배제 — 이 단어가 있으면 특약 제목 아님
_NON_COVERAGE = re.compile(
    r'용어|풀이|경우|명령|소비자|지났을|무효|금지|처리|제한|해당하는|예금자|가이드|목\s*차|요약|보호제도')
# 특약 제목: '###### N. 담보명' 또는 'N-1. 담보명'(갱신 변형)
_TITLE = re.compile(r'^\d+(-\d+)?\.\s{0,2}\S{2,}')


def is_kb_title(line: str) -> str | None:
    """마크다운 헤딩 줄이 KB 특약 제목이면 제목 텍스트, 아니면 None.
    번호형 담보명(`###### 3. 상해수술비(간편가입)`)만 — 준용규정 문장·조·용어/조건은 배제."""
    if not line.lstrip().startswith("#"):
        return None
    t = line.lstrip("# ").strip()
    if "정하지" in t or re.match(r'^제\s*\d+\s*조', t) or _NON_COVERAGE.search(t):
        return None
    return t if _TITLE.match(t) else None


def _is_subcontract(clauses: list[dict]) -> bool:
    """특약의 강한 신호 = 조 2개 이상 + 준용규정 조 보유(요약/용어 블록엔 준용규정 없음)."""
    return len(clauses) >= 2 and any("준용" in (c["title"] or "") for c in clauses)


def extract_subcontracts(recon_md: str) -> list[dict]:
    """재구성 마크다운 → title-driven 특약 리스트 [{name, clauses}].
    각 특약 제목 헤딩에서 다음 제목까지를 한 특약으로 잘라 parse_clauses."""
    titles, off = [], 0
    for ln in recon_md.split("\n"):
        if ln.lstrip().startswith("#"):
            t = is_kb_title(ln)
            if t:
                titles.append((off, t))
        off += len(ln) + 1
    out = []
    for i, (start, name) in enumerate(titles):
        end = titles[i + 1][0] if i + 1 < len(titles) else len(recon_md)
        clauses = pc.parse_clauses(recon_md[start:end], "KB")
        if _is_subcontract(clauses):
            out.append({"name": name, "clauses": clauses})
    return out


def extract_from_json(json_path: str) -> list[dict]:
    return extract_subcontracts(rr.reconstruct(json_path))


def ingest_sections(json_path: str):
    """적재용: (재구성 md, sections) — ingest_compound이 recon_md에 region으로 기존 로더 재사용.
    보통약관(첫 특약 title 전)=부모, 각 특약 title→다음 title=자식. load_clauses.build_sql(recon,
    pid, region)이 region으로 parse_clauses해 회사미상과 동일 경로로 적재된다(코드 재사용)."""
    recon = rr.reconstruct(json_path)
    titles, off = [], 0
    for ln in recon.split("\n"):
        if ln.lstrip().startswith("#"):
            t = is_kb_title(ln)
            if t:
                titles.append((off, t))
        off += len(ln) + 1
    first = titles[0][0] if titles else len(recon)
    sections = [{"name": None, "parent": False, "region": (0, first)}]     # 보통약관=부모
    for i, (start, name) in enumerate(titles):
        end = titles[i + 1][0] if i + 1 < len(titles) else len(recon)
        if _is_subcontract(pc.parse_clauses(recon[start:end], "probe")):
            sections.append({"name": name, "parent": True, "region": (start, end)})
    return recon, sections


def main():
    if len(sys.argv) < 2:
        print("용법: kb_parse.py <KB문서명>", file=sys.stderr)
        sys.exit(2)
    doc = sys.argv[1]
    import glob
    cand = [p for p in glob.glob(os.path.join(HERE, "..", "data", "output", "raw", "*.json"))
            if doc in os.path.basename(p)]
    if not cand:
        print(f"문서 없음: {doc}", file=sys.stderr)
        sys.exit(1)
    subs = extract_from_json(cand[0])
    total = sum(len(s["clauses"]) for s in subs)
    print(f"\n{os.path.basename(cand[0])[:-5]} — 특약 {len(subs)}개 · 총 {total}조 (title-driven, LLM 없이)")
    print("-" * 76)
    for s in subs:
        cl = s["clauses"]
        rng = f"제{cl[0]['jo']}~{cl[-1]['jo']}조" if cl else "-"
        print(f"  {s['name'][:50]:<52} {len(cl):>2}조 {rng}")


if __name__ == "__main__":
    main()
