"""청킹 heading_path 품질 — TOC(목차) 조상 제외.

ODL이 "목차"를 얕은 heading 레벨로 뱉으면 실제 약관 본문(제N조)이 그 자식으로 잘못
중첩돼 거의 전 청크의 heading_path가 "…> 목차 >…"가 된다. heading_path는 임베딩·리랭커에
함께 실리므로 균일한 "목차" 토큰이 전 벡터를 희석. _heading_chain이 TOC 노드를 조상
경로에서 걷어내는지 검증한다(트리·content는 불변).
"""

from src.v1.utils.chunker_adaptive import chunk_markdown, _build_tree, _heading_chain


_MD = """# 무배당 THE 특약 (갱신형)
## 목차
### 보장한도
보장한도 요약 표 내용이 여기에 있다. 담보명 보장한도 일당 지급.
### 무배당 THE 특약 약관
#### 제2관 보험금의 지급
##### 제10조 【보험금 등의 청구】
보험수익자는 다음의 서류를 제출하고 보험금을 청구하여야 합니다.
##### 제11조 【보험금 등의 지급절차】
회사는 서류를 접수한 때부터 지급절차를 진행한다.
"""


def _find(node, kw):
    if kw in node.heading:
        return node
    for c in node.children:
        r = _find(c, kw)
        if r:
            return r
    return None


def test_heading_chain_excludes_toc_ancestor():
    """목차 아래로 잘못 중첩된 제10조 — heading_chain에 목차가 없어야 하고 실제 조는 보존."""
    root = _build_tree(_MD)
    chain = _heading_chain(_find(root, "제10조"))
    assert not any("목차" in h for h in chain), f"목차가 조상 경로에 남음: {chain}"
    assert any("제10조" in h for h in chain), "실제 조 heading이 유실됨"
    assert any("제2관" in h for h in chain), "상위 관(款) heading이 유실됨"


def test_toc_own_content_still_dropped():
    """목차 노드 자신의 content는 여전히 청킹 제외(기존 동작 무회귀)."""
    chunks = chunk_markdown(_MD, source_file="t.md", service_code="01")
    # 어떤 청크의 heading_path에도 '목차'가 조상으로 남지 않아야 함
    for c in chunks:
        path = c.metadata.get("heading_path", [])
        hp = path if isinstance(path, str) else " > ".join(path)
        assert "목차" not in hp, f"heading_path에 목차 잔존: {hp}"


def test_real_article_content_indexed():
    """실제 조 본문은 정상 청킹되어야 함(과잉 필터 방지)."""
    chunks = chunk_markdown(_MD, source_file="t.md", service_code="01")
    joined = " ".join(c.content for c in chunks)
    assert "서류를 제출" in joined, "제10조 본문 유실"
    assert "지급절차를 진행" in joined, "제11조 본문 유실"
