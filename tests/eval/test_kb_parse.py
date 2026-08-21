"""KB 특약 추출(kb_parse) 순수 로직 잠금 — title 판별 + title-driven 세그먼테이션.

KB는 재구성이 특약 제목(`###### N. 담보명`)을 제1조 앞에 놓아야 열린다. 핵심 계약: 번호형
담보명 헤딩만 특약 제목으로 인정(용어/조건/준용문장 배제), 각 제목→다음 제목까지가 한 특약,
준용규정 조 보유가 특약 판별식. 이 세 성질을 못박는다(LLM 없이 KB가 열리는 근거).
"""
from scripts.kb_parse import is_kb_title, _is_subcontract, extract_subcontracts


def test_numbered_coverage_is_title():
    """`###### N. 담보명`은 특약 제목."""
    assert is_kb_title("###### 1. 장기요양간병비(1~5급)(간편가입)") == "1. 장기요양간병비(1~5급)(간편가입)"
    assert is_kb_title("###### 3. 상해수술비(간편가입)") == "3. 상해수술비(간편가입)"
    assert is_kb_title("###### 8-1. 상해입원일당Ⅱ") == "8-1. 상해입원일당Ⅱ"   # 갱신 변형


def test_non_coverage_headings_rejected():
    """용어·조건·준용문장·조·비헤딩은 제목 아님(precision)."""
    assert is_kb_title("###### 5. 기타 관련 용어") is None            # 용어
    assert is_kb_title("###### 3. 최초 계약을 체결한 날부터 3년이 지났을 때") is None  # 조건
    assert is_kb_title("제6조(준용규정) 이 특별약관에서 정하지 않은 사항은") is None   # 준용 문장/조
    assert is_kb_title("## 보통약관") is None                        # 번호 없음
    assert is_kb_title("1. 상해수술비(간편가입)") is None              # 헤딩 아님(# 없음)


def test_subcontract_needs_junyong_and_two_clauses():
    """특약 판별식: 조 2개 이상 + 준용규정 조."""
    assert _is_subcontract([{"title": "보험금의 지급사유"}, {"title": "준용규정"}]) is True
    assert _is_subcontract([{"title": "보험금의 지급사유"}, {"title": "보험금의 종류"}]) is False  # 준용 없음
    assert _is_subcontract([{"title": "준용규정"}]) is False          # 조 1개


_RECON = (
    "###### 제1장 상해 관련 특별약관\n"
    "###### 1. 상해수술비(간편가입)\n"
    "제1조(보험금의 지급사유) 회사는 상해로 수술 시 보험금을 지급합니다.\n"
    "제2조(준용규정) 이 특별약관에서 정하지 않은 사항은 보통약관을 따릅니다.\n"
    "###### 2. 골절진단비(간편가입)\n"
    "제1조(보험금의 지급사유) 회사는 골절 진단 시 보험금을 지급합니다.\n"
    "제2조(준용규정) 이 특별약관에서 정하지 않은 사항은 보통약관을 따릅니다.\n"
)


def test_title_driven_segmentation():
    """각 번호형 제목 → 다음 제목까지 한 특약. 그룹헤딩(제N장)은 번호형 아니라 경계 아님."""
    subs = extract_subcontracts(_RECON)
    names = [s["name"] for s in subs]
    assert names == ["1. 상해수술비(간편가입)", "2. 골절진단비(간편가입)"]
    assert all(len(s["clauses"]) == 2 for s in subs)     # 각 특약 제1·2조
