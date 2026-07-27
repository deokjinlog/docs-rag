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

# 조 정의 — 회사/포맷별 2 프로파일 (단일 정규식은 불가: 반각()은 조 정의와 인라인 참조에
# 모두 쓰여 참조까지 다 잡음 — 실측 New치아 222조·다이렉트 179조).
#   full(전각): "제N조 【제목】" — 라이나 계열. 인라인 참조가 반각()이라 【】-전용이 깨끗.
#   half(반각): 줄머리 "제N조(제목) <본문>" — New치아·다이렉트. 인라인 참조는 줄 중간이라
#     라인시작 앵커로 배제, 목차(괄호 뒤 본문 없음)는 본문길이로 배제, 인용법령 전문(제1조
#     부터 재시작)은 번호 단조증가 가드로 배제.
RE_JO = re.compile(r'^[#\s\-•*]*제\s*(\d+)\s*조\s*【\s*([^】]+?)\s*】', re.MULTILINE)
# 반각 제목은 중첩 괄호를 가짐(예: 치아우식증(충치) 및 치주질환(잇몸질환)…) → 1단계 중첩 허용
RE_JO_HALF = re.compile(
    r'^[#\s\-•*]*제\s*(\d+)\s*조\s*\(\s*((?:[^()\n]|\([^()\n]*\))*?)\s*\)', re.MULTILINE)
# 목차(ToC) 항목: 제목 뒤에 점선 리더(………11)가 붙는다 → 본문 조와 구분해 스킵
RE_TOC_DOTS = re.compile(r'[.·․…]{3,}')
# 부록 시작(별표/부칙): 마지막 조의 본문은 여기서 끝난다. 【별표N】(전각) 헤딩도 포함.
RE_APPENDIX_START = re.compile(
    r'^#{0,4}\s*[（(【]?\s*별\s*표\s*\d+|^#{0,4}\s*부\s*칙\b', re.MULTILINE)
# 항: 원문자 ①~⑳ (줄머리)
RE_HANG = re.compile(r'^[\s\-•*]*([①-⑳])', re.MULTILINE)
# 외부 법령: 마스킹 대상 (내부 조항 참조와 구분)
RE_EXTERNAL = re.compile(
    r'(의료법시행규칙|동법시행규칙|의료법|동법|상법|민법|보험업법|근로기준법|약관의?\s*규제[^\s]*)'
    r'\s*제\s*\d+\s*조'
)
# 약관 절(節) 경계: "제 N 절" (OCR가 글자 사이 공백을 넣어 제\s*N\s*절). New치아·다이렉트는
# 제1절 보통약관 + 제2절 특별약관(수십 개, 각자 제1조부터 재시작)인 복합 문서라, 이 경계로
# 보통약관만 먼저 잘라 파싱한다(특별약관은 각각 미니상품 → 후속).
RE_SECTION = re.compile(r'^#{1,6}\s*제\s*(\d+)\s*절', re.MULTILINE)
# 별표 섹션 헤더: 라인시작 "(별표N)" — 불릿(- (별표1)) / 헤딩(## (별표3)) 양쪽.
# 괄호 필수 → 인라인 "별표4(재해분류표)" 참조는 줄 중간이라 배제. ToC 점선 항목은
# find_annexes에서 RE_TOC_DOTS로 스킵. (ODL이 별표1·2는 불릿, 3·4는 헤딩으로 뽑음)
RE_ANNEX = re.compile(r'^[#\-•*\s]*[（(]\s*별표\s*(\d+)\s*[)）]\s*(.*)$', re.MULTILINE)


def _annex_kind(title: str) -> str:
    """별표 종류: 분류표(행 단위 어휘검색) / 적립이율(공식) / 지급기준표(통째 컨텍스트)."""
    if "분류표" in title:
        return "classification"
    if "이율" in title or "계산" in title:
        return "formula"
    return "payout"                       # 지급기준표 등 (기본)

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def select_profile(md: str) -> str:
    """전각【】가 여럿이면 full(라이나), 아니면 half(반각: New치아·다이렉트)."""
    return "full" if len(RE_JO.findall(md)) >= 3 else "half"


def parse_clauses(md: str, product_id: str) -> list[dict]:
    """조 경계로 분할 → 조 단위 clause 리스트. 외부법령 마스킹(위치보존) 후 탐지,
    본문은 원본에서. 목차/부록/인용법령 전문은 프로파일별로 배제, 번호 단조증가로 본문 한정."""
    profile = select_profile(md)
    regex = RE_JO if profile == "full" else RE_JO_HALF

    # 의료법 제N조 등 외부법령을 같은 길이 filler로 치환 → 위치 보존, 조 오탐 방지
    md_masked = RE_EXTERNAL.sub(lambda m: "␡" * len(m.group()), md)

    # 복합 문서(제1절 보통약관 + 제2절~ 특별약관)면 보통약관 구간만 파싱. 그러면 목차(제1절
    # 앞)와 특별약관(제2절 뒤)이 구간 밖이라 자동 배제 → ToC 필터 없이 라인시작+단조증가로 충분.
    sections = []                                       # 목차의 "제2절 ····" 항목은 제외
    for m in RE_SECTION.finditer(md_masked):
        le = md_masked.find("\n", m.start())
        if not RE_TOC_DOTS.search(md_masked[m.start(): le if le != -1 else len(md_masked)]):
            sections.append(m)
    compound = len(sections) >= 2
    if compound:
        body_start, body_end = sections[0].end(), sections[1].start()
    else:
        app = RE_APPENDIX_START.search(md_masked)
        body_start, body_end = 0, (app.start() if app else len(md))

    jos, last = [], 0
    for m in regex.finditer(md_masked):
        if not (body_start <= m.start() < body_end):   # 구간(보통약관 or 본문) 밖 제외
            continue
        line_end = md_masked.find("\n", m.end())
        rest = md_masked[m.end(): line_end if line_end != -1 else len(md_masked)]
        if not compound:                                # 단일 문서: 구간 내 목차가 섞여 필터 필요
            if profile == "full" and RE_TOC_DOTS.search(rest):
                continue                                # 전각: 목차 점선 항목 제외
            if profile == "half" and len(rest.strip()) < 10:
                continue                                # 반각: 목차(괄호 뒤 본문 없음) 제외
        jo = int(m.group(1))
        # 번호 리셋/역행 = 인용법령 전문(공휴일 규정 등 제1조부터 재시작) 시작 → 본문 끝
        if jos and jo <= last:
            break
        jos.append(m)
        last = jo

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


def _dedup_title(t: str) -> str:
    """ODL이 헤딩 제목을 중복 반복함('재해분류표 재해분류표') → 절반 반복 제거."""
    t = re.sub(r'\s+', ' ', t).strip()
    half = len(t) // 2
    if len(t) % 2 == 1 and t[half] == ' ' and t[:half] == t[half + 1:]:
        return t[:half]
    return t


def find_annexes(md: str, product_id: str) -> list[dict]:
    """별표 섹션 탐지 → 경계·본문·종류. 라인시작 (별표N) 마커만(인라인 참조 배제),
    ToC 점선 항목 스킵. 각 섹션 본문은 다음 별표 전까지. fetch 경로용(검색 아님)."""
    marks = []
    for m in RE_ANNEX.finditer(md):
        line_end = md.find("\n", m.end())
        rest = md[m.end(): line_end if line_end != -1 else len(md)]
        if RE_TOC_DOTS.search(m.group(0)) or RE_TOC_DOTS.search(rest):
            continue                          # 목차 점선 항목 제외
        marks.append((m.start(), int(m.group(1)), _dedup_title(m.group(2))))

    out = []
    for i, (start, no, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(md)
        out.append({
            "annex_id": f"{product_id}_별표{no}", "no": no, "title": title,
            "kind": _annex_kind(title), "raw_markdown": md[start:end].strip(),
        })
    return out


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
