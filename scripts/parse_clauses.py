"""조/항/호 계층 파서 (프로토타입) — 약관 마크다운에서 조문 구조를 재구성.

ODL이 일부 조는 마크다운 헤딩(#### 제14조), 일부는 불릿 평문(- 제5조)으로 뽑아
일관성이 없다. 그래서 헤딩을 믿지 않고 **정규식으로 원문 전체를 재스캔**해 조 경계를
잡는다. 또한 "의료법 제3조" 같은 외부 법령 참조를 먼저 마스킹해, 약관 제3조로
오연결되는 오탐을 막는다.

용법: python scripts/parse_clauses.py <md경로> [product_id]
"""
import re
import sys
import json

# 조 정의: 줄머리의 "제N조 【제목】" (전각 【】 프로파일 — 라이나 계열).
# 회사별로 괄호가 다르다: 라이나=전각【】, New치아·다이렉트=반각( ). 반각은 조 정의와
# 인라인 참조에 모두 쓰여 단일 정규식이 참조까지 다 잡으므로(실측: New치아 222조),
# 프로파일을 분리한다. 전각은 인라인 참조가 반각()이라 【】-전용이 깨끗이 걸러진다.
# → PARSER_PROFILES 로 회사/포맷별 규칙 분리 (반각 프로파일은 후속: 정의=본문 순차런).
RE_JO = re.compile(
    r'^[#\s\-•*]*제\s*(\d+)\s*조\s*【\s*([^】]+?)\s*】',
    re.MULTILINE,
)
# 목차(ToC) 항목: 제목 뒤에 점선 리더(………11)가 붙는다 → 본문 조와 구분해 스킵
RE_TOC_DOTS = re.compile(r'[.·․…]{3,}')
# 부록 시작(별표/부칙): 마지막 조의 본문은 여기서 끝난다
RE_APPENDIX_START = re.compile(r'^#{0,4}\s*[（(]?\s*별표\s*\d+|^#{0,4}\s*부\s*칙\b', re.MULTILINE)
# 항: 원문자 ①~⑳ (줄머리)
RE_HANG = re.compile(r'^[\s\-•*]*([①-⑳])', re.MULTILINE)
# 외부 법령: 마스킹 대상 (내부 조항 참조와 구분)
RE_EXTERNAL = re.compile(
    r'(의료법시행규칙|동법시행규칙|의료법|동법|상법|민법|보험업법|근로기준법|약관의?\s*규제[^\s]*)'
    r'\s*제\s*\d+\s*조'
)
# 별표 헤딩: "## (별표3) 질병 및 재해분류표"
RE_ANNEX = re.compile(r'^#{1,4}\s*[（(]?\s*별표\s*(\d+)\s*[)）]?\s*(.*)$', re.MULTILINE)

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def parse_clauses(md: str, product_id: str) -> list[dict]:
    """조 경계로 분할 → 조 단위 clause 리스트. 외부법령 마스킹(위치보존) 후 탐지,
    본문은 원본에서. ToC 항목 스킵, 본문은 별표 전까지."""
    # 의료법 제N조 등 외부법령을 같은 길이 filler로 치환 → 위치 보존, 조 오탐 방지
    md_masked = RE_EXTERNAL.sub(lambda m: "␡" * len(m.group()), md)
    app = RE_APPENDIX_START.search(md_masked)
    body_end = app.start() if app else len(md)

    jos = []
    for m in RE_JO.finditer(md_masked):
        line_end = md_masked.find("\n", m.end())
        rest = md_masked[m.end(): line_end if line_end != -1 else len(md_masked)]
        if RE_TOC_DOTS.search(rest):      # 목차 점선 항목 제외
            continue
        if m.start() >= body_end:         # 별표 이후는 조 아님
            continue
        jos.append(m)

    out = []
    for i, m in enumerate(jos):
        jo = int(m.group(1))
        title = m.group(2).strip()
        start = m.start()
        end = jos[i + 1].start() if i + 1 < len(jos) else body_end
        out.append({
            "clause_id": f"{product_id}_제{jo}조",
            "jo": jo, "hang": None, "title": title,
            "parent_id": None, "text": md[start:end].strip(),
        })
    return out


def extract_refs(text: str, self_jo: int, product_id: str) -> list[dict]:
    """조 본문에서 참조 추출. 외부 법령 먼저 마스킹 → 내부 조항/항/별표만 남김."""
    masked = RE_EXTERNAL.sub(lambda m: "␡" * len(m.group()), text)
    refs = []
    seen = set()
    for m in re.finditer(r'제\s*(\d+)\s*조', masked):
        tj = int(m.group(1))
        if tj == self_jo:
            continue  # 자기 자신 참조 무시
        key = ("조항", tj)
        if key not in seen:
            seen.add(key)
            refs.append({"type": "조항", "target": f"{product_id}_제{tj}조"})
    for m in re.finditer(r'별표\s*(\d+)', masked):
        key = ("별표", m.group(1))
        if key not in seen:
            seen.add(key)
            refs.append({"type": "별표", "target": f"{product_id}_별표{m.group(1)}"})
    return refs


def find_annexes(md: str, product_id: str) -> list[dict]:
    return [{"annex_id": f"{product_id}_별표{m.group(1)}", "no": int(m.group(1)),
             "title": m.group(2).strip()} for m in RE_ANNEX.finditer(md)]


def main():
    md_path = sys.argv[1] if len(sys.argv) > 1 else "data/output/raw/라이나_중환자실입원특약.md"
    product_id = sys.argv[2] if len(sys.argv) > 2 else "LINA_ICU_2024"
    md = open(md_path, encoding="utf-8").read()

    clauses = parse_clauses(md, product_id)
    annexes = find_annexes(md, product_id)

    print(f"=== 조 파싱 결과 ({len(clauses)}개) ===")
    for c in clauses:
        refs = extract_refs(c["text"], c["jo"], product_id)
        ref_s = " ".join(f"{r['type']}:{r['target'].split('_')[-1]}" for r in refs)
        print(f"  제{c['jo']:>2}조 【{c['title']}】  {'→ ' + ref_s if ref_s else ''}")

    print(f"\n=== 별표 ({len(annexes)}개) ===")
    for a in annexes:
        print(f"  별표{a['no']}: {a['title']}")

    # ── 검증 2개 ──
    print("\n=== 검증 ===")
    jos = [c["jo"] for c in clauses]
    contiguous = jos == list(range(1, len(jos) + 1))   # 1..N 연속 = 깨끗한 파싱
    print(f"  [1] 조 {len(clauses)}개, 1..N 연속: {'✅' if contiguous else '❌ 깨짐'}")
    print(f"      조 번호: {jos}")
    if not contiguous:
        missing = sorted(set(range(1, max(jos) + 1)) - set(jos)) if jos else []
        dups = sorted({j for j in jos if jos.count(j) > 1})
        print(f"      누락: {missing} · 중복: {dups}")

    # 의료법 제3조 오탐 검증: 제3조(입원 정의) 본문에 '의료법 제3조'가 있는데,
    # extract_refs가 이를 약관 제3조 자기참조로 안 잡고 걸러내는지.
    c3 = next((c for c in clauses if c["jo"] == 3), None)
    if c3:
        raw_has = "의료법 제3조" in c3["text"] or "의료법제3조" in c3["text"].replace(" ", "")
        refs3 = extract_refs(c3["text"], 3, product_id)
        # 제3조 본문의 다른 외부법령이 내부참조로 새는지
        external_leak = any("제3조" in r["target"] for r in refs3)
        print(f"  [2] 의료법 외부참조 마스킹: 제3조 본문에 의료법 참조 존재={raw_has}, "
              f"내부참조 오탐={'❌ 있음' if external_leak else '✅ 없음(걸러짐)'}")


if __name__ == "__main__":
    main()
