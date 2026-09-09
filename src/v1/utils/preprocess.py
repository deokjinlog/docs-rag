"""마크다운 텍스트 전처리. 검색/임베딩용 정규화. 원문은 raw_markdown에 보존."""

from __future__ import annotations

import re
import unicodedata


PAGE_MARKER_RE = re.compile(r'^\s*<!--\s*page:(\d+)\s*-->\s*$')

# ── 마크다운 이미지 태그 (한 곳에 모아둔다) ──────────────────────────────────
# 이 패턴이 세 모듈(ocr 태스크·청커·여기)에 각각 복제돼 있었고, **서로 달라져서** 조용히
# 깨졌다. ODL 실제 출력은 `![](<경로.png>)` — 빈 alt 에 꺾쇠로 감싼 경로다. 그런데 OCR
# 태스크는 `![image N](경로.png)` 를 기대해 **한 건도 매칭되지 않았고**, 그래서 매 문서에서
# "이미지 없음"으로 조기 return 했다(paddle 은 파이프라인에서 호출된 적이 없다 →
# image 청크 0개). 청커의 넓은 스크러버가 잔해만 치워서 증상이 색인에 안 드러났다.
#
# 두 개인 이유가 있다:
#   IMAGE_TAG_RE      경로를 **캡처**한다 — OCR 대상 파일을 찾는 용도라 확장자를 제한한다
#   IMAGE_TAG_ANY_RE  경로를 안 본다 — 텍스트에서 태그를 **지우는** 안전망이라 넓어야 한다
IMAGE_TAG_RE = re.compile(
    r'!\[[^\]]*\]\(\s*<?\s*([^<>()\s]+?\.(?:png|jpg|jpeg|gif|bmp|tiff))\s*>?\s*\)',
    re.IGNORECASE,
)
IMAGE_TAG_ANY_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')


def is_page_marker(line: str) -> int | None:
    m = PAGE_MARKER_RE.match(line)
    return int(m.group(1)) if m else None


def extract_page_range(text: str) -> tuple[int | None, int | None]:
    pages = [p for line in text.split('\n') if (p := is_page_marker(line)) is not None]
    if not pages:
        return None, None
    return min(pages), max(pages)


def parse_heading(line: str) -> tuple[int, str] | None:
    m = re.match(r'^(#{1,6})\s+(.+)$', line.strip())
    if m:
        return len(m.group(1)), m.group(2).strip()
    return None


# 노이즈 필터 — PDF 변환 시 발생하는 비텍스트 잔해를 제거.
# 이 줄들이 남으면 청크에 포함되어 임베딩/검색 품질을 떨어뜨림.
def is_noise_line(line: str) -> bool:
    """줄 단위 노이즈 판별. True이면 청킹 전에 제거."""
    stripped = line.strip()
    if not stripped:
        return False
    # ODL이 이미지를 마크다운 태그로 변환한 잔해. OCR 태스크에서 별도 처리.
    if IMAGE_TAG_ANY_RE.match(stripped):
        return True
    # 목차 점선/말줄임 — PDF 목차의 "제1조 ··········· 15" 패턴
    if '·' * 5 in stripped or '…' * 3 in stripped:
        return True
    # 헤더/푸터 페이지 번호 패턴들 — PDF 상·하단 반복 문구
    if re.match(r'^[-–—]\s*\d+\s*[-–—]$', stripped):          # "– 15 –"
        return True
    if re.match(r'^\d+\s*[-–—]$', stripped):                   # "3 -"
        return True
    if re.match(r'^(page\s+)?\d+\s*/\s*\d+$', stripped, re.IGNORECASE):  # "3 / 10"
        return True
    if re.match(r'^page\s+\d+\s+of\s+\d+$', stripped, re.IGNORECASE):   # "page 3 of 10"
        return True
    # 1~2자 기호만 있는 줄 — 구분선 잔해, 글머리 기호 등
    if len(stripped) <= 2 and not re.search(r'[가-힣a-zA-Z0-9]', stripped):
        return True
    return False


def is_junk_table_block(lines: list[str]) -> bool:
    """단일 컬럼 테이블은 PDF 변환 시 발생하는 깨진 테이블. 임베딩 노이즈 방지를 위해 제거."""
    table_lines = [l for l in lines if l.strip().startswith('|') and l.strip().endswith('|')]
    if not table_lines:
        return False
    for tl in table_lines:
        inner = tl.strip().strip('|')
        if re.match(r'^[\s\-:]+$', inner):
            continue
        if inner.count('|') >= 1:
            return False
    return True


# TOC 제거
_TOC_HEADING_RE = re.compile(
    r'^(#{1,6})\s+(목\s*차|차\s*례|table\s+of\s+contents|contents)\s*$',
    re.IGNORECASE,
)

def remove_toc(text: str) -> str:
    """목차(TOC) 섹션 제거 — TOC 헤딩부터 다음 동일/상위 레벨 헤딩 전까지"""
    lines = text.split('\n')
    result = []
    skip = False
    toc_level = 0

    for line in lines:
        if skip:
            # 다음 동일/상위 레벨 헤딩을 만나면 스킵 종료
            heading = parse_heading(line)
            if heading and heading[0] <= toc_level:
                skip = False
                result.append(line)
            continue

        m = _TOC_HEADING_RE.match(line.strip())
        if m:
            toc_level = len(m.group(1))
            skip = True
            continue

        result.append(line)

    return '\n'.join(result)


# 정규화 — 검색/임베딩용. 원문은 tb_document_extract.raw_markdown에 보존되므로
# 여기서의 따옴표·대시 표준화는 원문 렌더링에 영향 주지 않는다.
def normalize_whitespace(text: str) -> str:
    """검색/임베딩용 텍스트 정규화. 같은 의미의 다른 표현을 통일해 벡터 유사도를 안정화."""
    text = unicodedata.normalize('NFKC', text)
    # 유령 공백 제거
    text = text.replace('\xa0', ' ')
    text = text.replace('\u200b', '')
    text = text.replace('\u3000', ' ')
    text = text.replace('\u200c', '')
    text = text.replace('\u200d', '')
    text = text.replace('\ufeff', '')
    # 따옴표 표준화: 꺾쇠·겹낫·유니코드 따옴표 → 직선 따옴표
    for ch in '「」『』\u201c\u201d\u201e\u201f\u00ab\u00bb':
        text = text.replace(ch, '"')
    for ch in '\u2018\u2019\u201a\u201b':
        text = text.replace(ch, "'")
    # 대시 표준화: 유니코드 대시 → ASCII hyphen-minus(U+002D)
    for ch in '\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d':
        text = text.replace(ch, '-')
    # 공백·개행 정규화
    text = text.replace('\t', '    ')
    text = re.sub(r'[·]{3,}', ' ', text)       # 연속 가운뎃점(···) → 공백
    text = re.sub(r'[…]{2,}', ' ', text)       # 연속 말줄임(……) → 공백
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)          # 다중 공백 정리
    return text


# 클리닝
def clean_text(text: str) -> str:
    """청킹 전 최종 정리. 깨진 테이블/노이즈 줄을 제거해 청크 품질 보장."""
    raw_lines = text.split('\n')
    block_filtered: list[str] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        stripped = line.strip()
        if stripped.startswith('|') and stripped.endswith('|'):
            table_block: list[str] = []
            while i < len(raw_lines):
                s = raw_lines[i].strip()
                if s.startswith('|') and s.endswith('|'):
                    table_block.append(raw_lines[i])
                    i += 1
                else:
                    break
            if not is_junk_table_block(table_block):
                block_filtered.extend(table_block)
        else:
            block_filtered.append(line)
            i += 1

    lines: list[str] = []
    for line in block_filtered:
        if is_page_marker(line) is not None:
            continue
        if is_noise_line(line):
            continue
        line = re.sub(r'^#{7,}\s+', '', line)
        lines.append(line)

    return '\n'.join(lines).strip()
