"""페이지 글자가 색인까지 갔나 — **누락과 재배열을 가른다**.

**왜 이 구분이 필요한가.** V1 의 글자 커버리지가 R10 을 FAIL 로 찍었을 때 원인을 보려고
PyMuPDF 의 줄이 색인에 통째로 들어 있는지로 셌다. 그랬더니 면책 조항 본문이 사라진 것처럼
보였다 — `⑦ 직장 또는 항문질환 중…`, `단순포경(phimosis)`. 실제로는 **전부 색인에 있었다.**
줄 단위 포함 검사는 손실을 **과장한다.**

과장의 두 가지 원인:
  ① ODL 은 표를 `|셀|셀|` 로 뱉는다. PDF 의 한 줄이 표의 한 행이면 사이에 파이프가 끼어
     연속 부분문자열이 깨진다 → `norm` 이 파이프·리스트 표지를 지우는 이유.
  ② ODL 이 조각을 **다른 순서**로 내놓을 수 있다(다단·표를 열 순서로 읽는 경우).
     이건 파이프를 지워도 안 풀린다 → 조각 단위로 다시 확인한다.

판정은 **최장 일치 구간으로 줄을 덮어** 내린다. 줄을 왼쪽부터 훑으며 색인에서 찾을 수 있는
가장 긴 구간을 떼어내고, 못 떼는 자리를 구멍으로 모은다:

    구간 1개로 다 덮인다                  → kept    (그대로 살았다)
    구간 여러 개로 다 덮인다               → split   (글자는 다 있는데 흩어져 있다)
    못 덮는 구멍이 남는다                  → missing (진짜로 빠졌다 — 그 글자를 보여준다)

창(window)으로 세지 않는 이유: 조각이 갈린 자리를 **걸치는** 창은 반드시 없으므로, 흩어진
것과 빠진 것이 구분되지 않는다. 실제로 그렇게 짰다가 R10 의 정상 면책 조항을 손실로 고발했다.

비교 대상은 ODL 트리가 아니라 **그 페이지에 걸친 색인 청크**다. 답해야 할 질문이 "ODL 이
읽었나"가 아니라 *"검색이 이걸 찾을 수 있나"* 라서 — 청크에 없으면 없는 것이다.

⚠ `reorder` 는 "손실 아님"이지 "문제 없음"이 아니다. 순서가 뒤집히면 조문 읽기 순서가
깨지고, 그건 이 화면이 아니라 **읽기 순서 검증**이 잴 축이다(아직 없음).
"""
from __future__ import annotations

import html
import re
import unicodedata

# 구간으로 인정할 최소 길이. 짧게 잡으면 흔한 어미("합니다")로 아무 데나 붙어 구멍이
# 사라지고, 길게 잡으면 정상적으로 갈린 표 셀이 구멍이 된다.
MIN_SEG = 8

# 이 길이 미만의 구멍은 글리프 표기차(⑦ vs ⑦, 곡선따옴표)로 보고 손실로 세지 않는다.
MIN_GAP = 6

# 이보다 짧은 줄은 판정하지 않는다 — 페이지번호("89")·세로탭 한 글자("도")처럼
# 조판 부스러기가 대부분이라, 넣으면 화면이 잡음으로 찬다.
MIN_LINE = 6

# ODL 이 넣은 마크다운 리스트 표지. 원문의 하이픈(C00-C97)까지 지우면 안 되므로
# **줄머리에서 뒤에 공백이 오는 것만** 지운다.
_MD_LIST = re.compile(r"(?m)^[ \t]*[-*+]\s+")

# 공백 + 표 파이프 + **조판용 따옴표·가운뎃점**. 마지막 묶음이 필요한 이유는 실측이다 —
# 같은 조 제목이 PDF 에선 `“간편심사”`·`제8차 개정 한국표준질병‧사인분류`, ODL 출력에선
# 다른 코드포인트로 나온다(곡선따옴표 4종, 가운뎃점 U+2027/U+00B7/U+2219). NFKC 는 이들을
# 통일하지 않아서, 안 지우면 **정상 조문이 구멍으로 찍힌다**. 파싱 골든이 "특수문자 정의
# 제목"을 취약 지점으로 잠가둔 것과 같은 자리다.
# 코너 괄호 「」 도 같은 이유 — PDF 의 「국민건강보험법」 이 색인엔 "국민건강보험법" 으로
# 들어 있다(실측 R10 #7400: U+300C → U+0022). 파이프라인 어딘가의 정규화이고, 안 접으면
# 법령 인용이 들어간 **모든 줄이 누락으로 찍힌다** — R10 의 '면책 항목 실종'이 이것이었다.
_NOISE = re.compile(r"[\s|“”‘’\"'`´„‟「」『』〈〉《》·‧∙•・․]+")


def norm(s: str) -> str:
    """비교 축 정규화.

    `html.unescape` 가 필요한 이유도 실측이다 — ODL 이 `<최초계약의 경우>` 를
    `&lt;최초계약의 경우&gt;` 로 저장한다. 안 풀면 **표의 조건 라벨이 통째로 누락으로
    찍히고**, 그건 "최초계약 표가 사라졌다"는 잘못된 경보가 된다.
    """
    t = html.unescape(s or "")
    return _NOISE.sub("", unicodedata.normalize("NFKC", _MD_LIST.sub("", t)))


def _longest_match(n: str, i: int, hay: str) -> int:
    """`n[i:]` 의 접두 중 hay 안에 있는 **가장 긴 것**의 길이. 접두는 단조라 이분탐색."""
    lo, hi = 0, len(n) - i
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if n[i:i + mid] in hay:
            lo = mid
        else:
            hi = mid - 1
    return lo


def cover(n: str, hay: str) -> tuple[int, list[str]]:
    """`(구간 수, 못 덮은 구멍들)`."""
    i, segs, gaps = 0, 0, []
    while i < len(n):
        m = _longest_match(n, i, hay)
        if m >= MIN_SEG:
            segs += 1
            i += m
            continue
        j = i                                   # 구멍 시작 — 다시 붙을 수 있는 자리까지 민다
        while j < len(n) and _longest_match(n, j, hay) < MIN_SEG:
            j += 1
        gaps.append(n[i:j])
        i = j
    return segs, gaps


def classify_line(line: str, haystack_norm: str) -> tuple[str, list[str]]:
    """`(상태, 구멍들)`. 상태 ∈ kept | split | missing | skip."""
    n = norm(line)
    if len(n) < MIN_LINE:
        return "skip", []
    if n in haystack_norm:
        return "kept", []
    segs, gaps = cover(n, haystack_norm)
    real = [g for g in gaps if len(g) >= MIN_GAP]
    if real:
        return "missing", real
    return ("split" if segs > 1 else "kept"), []


def page_report(pdf_text: str, chunk_texts: list[str]) -> dict:
    """PyMuPDF 페이지 텍스트 vs 그 페이지에 걸친 청크들 → 판정 요약.

    ⚠ `chunk_texts` 에는 **heading·heading_path 도 넣어야 한다.** 조 제목은 `content` 가
    아니라 별도 컬럼에 있어서, content 만 대면 `제8조 【보험금 지급에 관한 세부규정】` 같은
    정상 제목이 전부 누락으로 찍힌다(실측: R04 p.8 에서 그렇게 오판했다).
    """
    hay = norm(" ".join(chunk_texts))
    counts = {"kept": 0, "split": 0, "missing": 0, "skip": 0}
    missing_lines: list[tuple[str, list[str]]] = []
    for line in (pdf_text or "").splitlines():
        st, gaps = classify_line(line, hay)
        counts[st] += 1
        if st == "missing":
            missing_lines.append((line.strip(), gaps))
    judged = counts["kept"] + counts["split"] + counts["missing"]
    return {
        **counts,
        "judged": judged,
        # 색인 도달률 — 흩어진 것은 손실이 아니므로 kept 와 같이 센다.
        "reached": round((counts["kept"] + counts["split"]) / judged, 3) if judged else None,
        "missing_lines": missing_lines[:12],
        "missing_more": max(0, len(missing_lines) - 12),
    }
