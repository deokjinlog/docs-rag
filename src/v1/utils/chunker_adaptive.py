"""Adaptive 청킹.
헤딩(h1~h6)과 문단 경계를 존중하여 문서 구조를 유지하면서 분할.
청킹 단계에서 Text/Table을 독립 타입으로 분리하여 각각 처리.
텍스트는 TEXT_MAX_CHARS 기준 분할 + 자투리 병합, 테이블은 완전한 단위로 독립 보존.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field

from .preprocess import (
    normalize_whitespace, clean_text,
    is_page_marker, extract_page_range, parse_heading,
    IMAGE_TAG_ANY_RE,
)
from ..config import TEXT_MAX_CHARS, TABLE_MAX_CHARS, CHUNK_MIN_CHARS

_TABLE_SEPARATOR_RE = re.compile(r'^\|[\s\-:|]+\|$')

# ODL 변환 편차 보정 — 조 제목이 heading이 아니라 리스트(`- 제15조 【…】`)/평문으로 오는 문서를
# 청킹 전에 바로잡는다. heading은 markdown heading 라인에서만 나오므로(parse_heading 전제),
# 소스를 고쳐야 조 단위 청킹·sibling 복원이 산다. 실측: 라이나 간병인 특약이 제13~15조를 리스트로,
# 문장 꼬리 "기준으로 합니다."를 H6로 뱉어 갱신 조가 쓰레기 heading을 상속했다.
_HEADING_LINE_RE = re.compile(r'^(#{1,6})\s+(.*)$')
# 조 제목은 회사별로 【제목】(라이나) 또는 (제목)(다이렉트·New치아) 표기. 소괄호는 본문 참조
# ("제3조(지급사유)에서 정한…")와 겹치므로, 닫는 괄호 뒤가 공백/줄끝일 때만 조 heading으로 승격
# (참조는 josa "에서/의/를…"가 바로 붙어 배제됨). 각괄호는 명확해 그 가드 없이 매칭.
_ARTICLE_TITLE_RE = re.compile(
    r'^\s*[-*]?\s*(제\s*\d+\s*조(?:\s*의\s*\d+)?\s*(?:【[^】]*】|\([^)]*\)(?=\s|$)))\s*(.*)$'
)
_ARTICLE_PROMOTE_LEVEL = 5                                   # #####  (실제 조 heading과 동일 레벨)
_LEADER_RE = re.compile(r'\.{4,}|·{5,}|…{3,}')               # 점선 목차 잔해(ASCII 마침표 포함 — normalize가 못 잡음)
_FRAGMENT_TAIL_RE = re.compile(r'(?:다|요|음|함|됨|임)\s*[.。]$')  # 문장 종결 꼬리(heading 오검출 신호)
_STRUCT_MARKER_RE = re.compile(r'제\s*\d+\s*[조관장절편]|별표|【')
# 미디어 노이즈 — 임베딩 텍스트 오염. 이미지 OCR은 별도 image 청크로 처리되므로 텍스트 청크의
# 마크다운 이미지 태그는 순수 잔해. OCR 태스크가 상류에서 지우지만(정규식 수정 후) 여기는
# **안전망으로 남긴다** — 확장자 없는 태그나 OCR 스킵 문서까지 받아내야 하므로 더 넓다.
_IMG_TAG_RE = IMAGE_TAG_ANY_RE
_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)

log = logging.getLogger(__name__)


def _repair_markdown_headings(text: str) -> str:
    """조 제목(리스트/평문) → heading 승격 · 문장꼬리 heading → 본문 강등 · 점선 목차줄 제거.
    ODL이 조 제목을 heading으로 뱉지 않는 문서에서 조 단위 grouping을 복구한다(precision-first:
    구조가 뚜렷한 조 제목·명백한 문장꼬리만 건드리고, 애매하면 원본 유지)."""
    text = _IMG_TAG_RE.sub('', text)      # 이미지 태그 잔해 제거(OCR은 별도 image 청크로 이미 처리됨)
    text = _BR_RE.sub(' ', text)          # <br> → 공백
    out: list[str] = []
    for line in text.split('\n'):
        s = line.strip()
        if _LEADER_RE.search(s):                              # 점선 목차 줄 제거(내비게이션·노이즈)
            continue
        h = _HEADING_LINE_RE.match(s)
        if h:
            htext = h.group(2).strip()
            # 문장 종결로 끝나고 구조 마커가 없으면 heading 오검출 → 본문으로 강등
            if _FRAGMENT_TAIL_RE.search(htext) and not _STRUCT_MARKER_RE.search(htext):
                out.append(htext)
            else:
                out.append(line)
            continue
        a = _ARTICLE_TITLE_RE.match(s)                        # 조 제목(리스트/평문) → heading 승격
        if a:
            out.append('#' * _ARTICLE_PROMOTE_LEVEL + ' ' + a.group(1).strip())
            rest = a.group(2).strip()
            if rest:                                          # 제목 뒤에 붙은 본문은 별도 줄로
                out.append(rest)
            continue
        out.append(line)
    return '\n'.join(out)


# 데이터 구조
@dataclass
class MdNode:
    heading: str
    level: int
    content: str = ""
    children: list["MdNode"] = field(default_factory=list)
    parent: MdNode | None = None
    page_at_heading: int | None = None


@dataclass
class Chunk:
    content: str          # 본문만 (heading_path는 임베딩/LLM 시점에 합침)
    heading: str
    metadata: dict = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.content)


# 유틸
def _heading_chain(node: MdNode) -> list[str]:
    # TOC 노드(목차·차례)는 조상 경로에서 제외한다. ODL이 "목차"를 얕은 레벨로 뱉으면
    # 실제 약관 본문(제N조)이 그 자식으로 잘못 중첩돼, 거의 전 청크의 heading_path가
    # "…> 목차 >…"가 된다(실측 R01·R02·R04 98~100%). heading_path는 임베딩·리랭커에
    # 함께 실리므로 균일한 "목차" 토큰이 전 벡터를 희석 — 조상 경로에서만 걷어낸다
    # (트리·content·chunk는 불변, 순수 경로 정제).
    chain = []
    cur = node
    while cur and cur.level > 0:
        if not _is_toc_heading(cur.heading):
            chain.append(cur.heading)
        cur = cur.parent
    chain.reverse()
    return chain


def _is_toc_heading(heading_text: str) -> bool:
    stripped = heading_text.strip()
    # "목차", "차례" 등 명시적 TOC 제목
    if re.match(r'^(목\s*차|차\s*례|table\s+of\s+contents|contents)$', stripped, re.IGNORECASE):
        return True
    # 점선+페이지번호 패턴이 포함된 TOC 스타일 헤딩 (예: "약관 이용 가이드북 ···15")
    if re.search(r'[·…]{3,}', stripped) or re.search(r'\s+\d+\s*$', stripped):
        if stripped.count('·') >= 3 or stripped.count('…') >= 2:
            return True
    return False




# 트리 빌드
def _build_tree(text: str) -> MdNode:
    root = MdNode(heading="(root)", level=0)
    stack: list[MdNode] = [root]
    current_page: int | None = None

    for line in text.splitlines():
        page = is_page_marker(line)
        if page is not None:
            current_page = page
            stack[-1].content += line + "\n"
            continue

        parsed = parse_heading(line)
        if parsed:
            level, heading_text = parsed
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            node = MdNode(
                heading=heading_text, level=level,
                parent=parent, page_at_heading=current_page,
            )
            parent.children.append(node)
            stack.append(node)
        else:
            stack[-1].content += line + "\n"

    return root


# 문단/문장 분할
def _split_sentences(text: str, max_chars: int) -> list[str]:
    sentences = re.split(r'(?<=\.)\s+', text)
    if len(sentences) <= 1:
        return [text]

    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if not cur:
            cur = sent
        elif len(cur) + len(sent) + 1 <= max_chars:
            cur += " " + sent
        else:
            chunks.append(cur)
            cur = sent
    if cur:
        chunks.append(cur)
    return chunks if chunks else [text]


def _split_paragraphs(text: str, max_chars: int) -> list[str]:
    merged_lines: list[str] = []
    in_table = False
    table_buf: list[str] = []

    for line in text.split('\n'):
        is_table_line = line.strip().startswith('|') and line.strip().endswith('|')
        if is_table_line:
            if not in_table:
                if merged_lines and merged_lines[-1].strip():
                    merged_lines.append('')
                in_table = True
            table_buf.append(line)
        else:
            if in_table:
                merged_lines.append('\n'.join(table_buf))
                merged_lines.append('')
                table_buf = []
                in_table = False
            merged_lines.append(line)

    if table_buf:
        merged_lines.append('\n'.join(table_buf))

    paras = re.split(r'\n\s*\n', '\n'.join(merged_lines))

    chunks: list[str] = []
    cur = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_split_sentences(para, max_chars))
            continue
        if not cur:
            cur = para
        elif len(cur) + len(para) + 2 <= max_chars:
            cur += "\n\n" + para
        else:
            chunks.append(cur)
            cur = para
    if cur:
        chunks.append(cur)
    return chunks


# 페이지 범위
def _resolve_page_range(node: MdNode, raw: str) -> tuple[int | None, int | None]:
    cs, ce = extract_page_range(raw)
    if cs is not None:
        ps = min(node.page_at_heading, cs) if node.page_at_heading is not None else cs
        return ps, ce
    if node.page_at_heading is not None:
        return node.page_at_heading, node.page_at_heading
    return None, None


def _assign_page_ranges(parts: list[str], raw: str, node: MdNode) -> list[tuple[int | None, int | None]]:
    page_at_positions: list[tuple[int, int]] = []
    offset = 0
    for line in raw.split('\n'):
        p = is_page_marker(line)
        if p is not None:
            page_at_positions.append((offset, p))
        offset += len(line) + 1

    if not page_at_positions:
        ps = node.page_at_heading
        return [(ps, ps) for _ in parts]

    cleaned_full = clean_text(raw)
    results: list[tuple[int | None, int | None]] = []
    search_start = 0

    for part in parts:
        idx = cleaned_full.find(part.strip(), search_start)
        if idx == -1:
            results.append(_resolve_page_range(node, raw))
            continue

        search_start = idx + len(part.strip())
        raw_len = len(raw)
        if len(cleaned_full) > 0:
            ratio_s = idx / len(cleaned_full)
            ratio_e = search_start / len(cleaned_full)
        else:
            ratio_s, ratio_e = 0.0, 1.0

        ps = pe = node.page_at_heading
        for char_off, page_num in page_at_positions:
            if char_off <= int(ratio_s * raw_len):
                ps = page_num
            if char_off <= int(ratio_e * raw_len):
                pe = page_num

        if ps is not None and pe is not None:
            results.append((min(ps, pe), max(ps, pe)))
        else:
            results.append((ps or pe, ps or pe))

    return results


# ── 표 캡션 ─────────────────────────────────────────────────────────────────
# 표 청크에는 **값만 있고 그 값이 무슨 조건의 값인지가 없다.** 실측(R05 제3조): 같은 모양의
# 지급표가 세 번 나오는데 하나는 "상해를 원인으로", 하나는 "질병을 원인으로", 하나는
# "재가입계약의 경우" 이고 값이 다르다(레진 50% vs 0·25·50%). 그런데 그 조건 라벨은 표
# 바로 앞 줄에 있어서 **다른 청크로 떨어지고**, heading_path 는 셋 다 같다. 표 하나만
# 회수되면 어느 조건인지 알 방법이 없다 — 검색도 LLM 도.
#
# 그래서 표 바로 앞 줄이 캡션이면 표 청크 본문 맨 앞에 붙인다. heading_path 가 아니라
# **content** 에 붙이는 이유: ①임베딩·리랭커가 그대로 먹는다 ②LLM 컨텍스트에 조건이 들어간다
# ③본문이 달라지므로 상해표와 질병표가 **중복으로 묶이지 않는다**(겹쳐 보이던 게 갈린다)
# ④heading_path 를 건드리면 part_index/sibling 복원의 그룹 키가 흔들린다.
#
# 임계 30자는 분포가 아니라 **정밀도 절벽**에서 왔다(표 청크 851, 앞줄 있는 것 683 실측):
#     ~30자  240건 — 전부 진짜 캡션("질병을 원인으로 …", "<최초계약의 경우>", "구분 기간 지급이자")
#     31~45   6건 — 반반("가. 의료급여기관 및 …범위"는 캡션, "4.1 2019.9.1 2019.10.1"은 표 데이터)
#     46~60   3건 — 전부 본문 조각("2 제1항에서 정하지 않은 용어의 뜻은 …")
# 9건을 포기하고 오염을 0으로 두는 쪽을 골랐다 — 잘못 붙은 캡션은 조건을 **거짓으로** 말한다.
_CAPTION_MAX_CHARS = 30
_CAPTION_MARKER_RE = re.compile(r'^\s*(?:[-*+•○◦▣□▪]|\d+[.)]|[가-힣][.)]|[①-⑳])\s*')
_CAPTION_SENT_END_RE = re.compile(r'(?:다|요|음|함|까)\s*[.。]?\s*$')     # 문장이면 캡션이 아니다


def _table_caption(line: str) -> str | None:
    """표 바로 앞 줄 → 캡션(붙일 문자열) 또는 None.

    `html.unescape` 를 쓰는 이유: 파이프라인이 `<최초계약의 경우>` 를 `&lt;…&gt;` 로 저장한다.
    그대로 붙이면 조건 라벨이 엔티티 문자열로 임베딩돼 신호가 죽는다.
    """
    t = _CAPTION_MARKER_RE.sub("", " ".join((line or "").split()))
    t = html.unescape(t).strip()
    if not t or len(t) > _CAPTION_MAX_CHARS:
        return None
    if _CAPTION_SENT_END_RE.search(t):
        return None
    if t.startswith("|") or t.startswith("#"):          # 표 행·헤딩은 캡션이 아니다
        return None
    return t


# 텍스트/테이블 분리
@dataclass
class _Segment:
    """노드 콘텐츠 내 텍스트 또는 테이블 블록."""
    content: str
    chunk_type: str  # "text" | "table"
    caption: str | None = None      # 표 세그먼트에만 — 바로 앞 줄에서 딴 조건 라벨


def _split_segments(body: str) -> list[_Segment]:
    """본문을 Text/Table 세그먼트로 분리. 문서 순서를 유지."""
    segments: list[_Segment] = []
    text_buf: list[str] = []
    table_buf: list[str] = []

    pending_caption: str | None = None
    for line in body.split('\n'):
        is_table_line = line.strip().startswith('|') and line.strip().endswith('|')
        if is_table_line:
            # 테이블 시작 → 누적된 텍스트를 먼저 flush. 이때 **마지막 줄이 캡션이면** 표로 넘긴다.
            if text_buf:
                joined = '\n'.join(text_buf).strip()
                lines = [l for l in joined.split('\n') if l.strip()]
                pending_caption = _table_caption(lines[-1]) if lines else None
                # 텍스트가 캡션 한 줄뿐이면 별도 청크로 내보내지 않는다 — 내용은 표에 실려
                # 살아 있고, 20자짜리 고아 청크는 검색에 잡음만 된다(실측 R05 #5679).
                if joined and not (pending_caption and len(lines) == 1):
                    segments.append(_Segment(content=joined, chunk_type="text"))
                text_buf = []
            table_buf.append(line)
        else:
            # 텍스트 시작 → 누적된 테이블을 먼저 flush
            if table_buf:
                joined = '\n'.join(table_buf).strip()
                if joined:
                    segments.append(_Segment(content=joined, chunk_type="table",
                                             caption=pending_caption))
                table_buf = []
                pending_caption = None
            text_buf.append(line)

    # 잔여 flush
    if table_buf:
        joined = '\n'.join(table_buf).strip()
        if joined:
            segments.append(_Segment(content=joined, chunk_type="table",
                                     caption=pending_caption))
    if text_buf:
        joined = '\n'.join(text_buf).strip()
        if joined:
            segments.append(_Segment(content=joined, chunk_type="text"))

    return segments


def _split_table(table_text: str, max_chars: int) -> list[str]:
    """큰 테이블을 행 단위로 분할. 헤더(컬럼명 + 구분선)를 매 청크마다 반복."""
    lines = table_text.split('\n')
    if len(lines) <= 2:
        return [table_text]

    # 헤더 추출: 첫 번째 행 + 구분선(|---|---|)
    header_lines: list[str] = [lines[0]]
    data_start = 1
    if len(lines) > 1 and _TABLE_SEPARATOR_RE.match(lines[1].strip()):
        header_lines.append(lines[1])
        data_start = 2

    header = '\n'.join(header_lines)
    header_len = len(header) + 1  # +1 for newline
    data_rows = lines[data_start:]

    if not data_rows:
        return [table_text]

    # 헤더 포함 전체가 상한 이내면 분할 불필요
    if len(table_text) <= max_chars:
        return [table_text]

    # 행 단위로 분할, 매 청크에 헤더 반복
    chunks: list[str] = []
    current_rows: list[str] = []
    current_len = header_len

    for row in data_rows:
        row_len = len(row) + 1
        if current_rows and current_len + row_len > max_chars:
            chunks.append(header + '\n' + '\n'.join(current_rows))
            current_rows = []
            current_len = header_len
        current_rows.append(row)
        current_len += row_len

    if current_rows:
        chunks.append(header + '\n' + '\n'.join(current_rows))

    return chunks


# 노드 → 청크 변환
def _chunk_node(node: MdNode, source_file: str, service_code: str = "") -> list[Chunk]:
    raw = node.content
    body = clean_text(raw)
    if not body:
        return []

    path = _heading_chain(node)
    node_ps, node_pe = _resolve_page_range(node, raw)
    base_meta = {
        "service_code": service_code,
        "source_file": source_file,
        "heading_path": path,
        "page_start": node_ps,
        "page_end": node_pe,
    }

    # Text/Table 독립 분리
    segments = _split_segments(body)

    # 각 세그먼트를 타입별로 청킹.
    # ⚠ 캡션은 여기서 붙이지 않는다. _assign_page_ranges 가 **원문에서 본문을 찾아** 페이지를
    #   매기는데, 원문에 없는 캡션(마커 제거·엔티티 해제됨)을 앞에 붙이면 find 가 실패해
    #   그 청크부터 페이지 귀속이 노드 단위로 뭉개진다. 캡션은 Chunk 를 만들 때 붙인다.
    all_parts: list[tuple[str, str, str | None]] = []  # (content, chunk_type, caption)

    for seg in segments:
        if seg.chunk_type == "table":
            # 테이블은 독립 보존. TABLE_MAX_CHARS 초과 시 행 단위 분할 (헤더 반복).
            # 캡션은 **분할된 조각마다** 붙는다 — 큰 표가 쪼개지면 뒤 조각이 조건을 잃는다.
            if len(seg.content) <= TABLE_MAX_CHARS:
                all_parts.append((seg.content, "table", seg.caption))
            else:
                for table_chunk in _split_table(seg.content, TABLE_MAX_CHARS):
                    all_parts.append((table_chunk, "table", seg.caption))
        else:
            # 텍스트는 TEXT_MAX_CHARS 기준 분할
            if len(seg.content) <= TEXT_MAX_CHARS:
                all_parts.append((seg.content, "text", None))
            else:
                parts = _split_paragraphs(seg.content, TEXT_MAX_CHARS)

                # 자투리 병합
                merged: list[str] = []
                carry = ""
                for part in parts:
                    if carry:
                        combined = carry + "\n\n" + part
                        if len(combined) <= TEXT_MAX_CHARS:
                            carry = combined
                            continue
                        merged.append(carry)
                    carry = part
                if carry:
                    if merged and len(merged[-1]) + len(carry) + 2 <= TEXT_MAX_CHARS:
                        merged[-1] += "\n\n" + carry
                    else:
                        merged.append(carry)

                # CHUNK_MIN_CHARS 미만 자투리를 이전 텍스트 청크에 강제 병합
                if len(merged) > 1 and len(merged[-1]) < CHUNK_MIN_CHARS:
                    merged[-2] += "\n\n" + merged[-1]
                    merged.pop()

                for m in merged:
                    all_parts.append((m, "text", None))

    # part_index 부여 (heading_path 내 문서 순서)
    total = len(all_parts)
    page_contents = [p[0] for p in all_parts]
    page_ranges = _assign_page_ranges(page_contents, raw, node)

    chunks: list[Chunk] = []
    for i, (content, chunk_type, caption) in enumerate(all_parts):
        text = content.strip()
        if not text:
            continue
        if caption:
            text = f"{caption}\n{text}"
        ps, pe = page_ranges[i] if i < len(page_ranges) else (node_ps, node_pe)
        chunks.append(Chunk(
            content=text, heading=node.heading,
            metadata={
                **base_meta,
                "page_start": ps, "page_end": pe,
                "chunk_type": chunk_type,
                "part_index": i + 1,
                "part_total": total,
            },
        ))

    return chunks


# 트리 순회
def _chunk_tree(root: MdNode, source_file: str, service_code: str = "") -> list[Chunk]:
    out: list[Chunk] = []

    def dfs(node: MdNode):
        if _is_toc_heading(node.heading):
            pass
        else:
            out.extend(_chunk_node(node, source_file, service_code))
        for child in node.children:
            dfs(child)

    # preamble (헤딩 전 텍스트)
    if root.content.strip():
        cleaned = clean_text(root.content)
        if cleaned:
            ps, pe = extract_page_range(root.content)
            out.append(Chunk(
                content=cleaned, heading="(preamble)",
                metadata={"service_code": service_code, "source_file": source_file, "page_start": ps, "page_end": pe},
            ))

    for child in root.children:
        dfs(child)

    return out


# 공개 API
def chunk_markdown(text: str, source_file: str = "", service_code: str = "") -> list[Chunk]:
    """메인 진입점: 마크다운 텍스트 → Chunk 리스트"""
    text = normalize_whitespace(text)
    text = _repair_markdown_headings(text)      # ODL heading 편차 보정(조 승격·문장꼬리 강등·점선 제거)
    tree = _build_tree(text)

    # 헤딩 없는 문서 → 통짜 1 chunk
    level_counts: dict[int, int] = {}
    def count(node: MdNode):
        if node.level > 0:
            level_counts[node.level] = level_counts.get(node.level, 0) + 1
        for c in node.children:
            count(c)
    count(tree)

    if not level_counts:
        cleaned = clean_text(text)
        return [Chunk(
            content=cleaned or text.strip(),
            heading="(no heading)",
            metadata={"service_code": service_code, "source_file": source_file, "page_start": None, "page_end": None},
        )]

    log.info(f"헤딩 분포: {dict(sorted(level_counts.items()))}, 총 {sum(level_counts.values())}개")
    return _chunk_tree(tree, source_file, service_code)


def to_json(chunks: list[Chunk]) -> list[dict]:
    """Chunk 리스트 → JSON 직렬화용 dict 리스트.
    content는 본문만 저장. heading_path는 임베딩/LLM 시점에 합침."""
    result = []
    for i, c in enumerate(chunks):
        meta = c.metadata
        item = {
            "service_code": meta.get("service_code", ""),
            "source_file": meta.get("source_file", ""),
            "page_start": meta.get("page_start"),
            "page_end": meta.get("page_end"),
        }

        # 헤딩 메타데이터 (adaptive/fixed 공통)
        if c.heading is not None:
            item["heading_path"] = meta.get("heading_path", [])
            item["heading"] = c.heading

        # adaptive 전용: chunk_type, part_index, part_total
        if "chunk_type" in meta:
            item["chunk_type"] = meta["chunk_type"]
        if "part_index" in meta:
            item["part_index"] = meta["part_index"]
            item["part_total"] = meta.get("part_total")

        item["chunk_id"] = i
        item["char_count"] = c.char_count
        item["content"] = c.content

        result.append(item)
    return result
