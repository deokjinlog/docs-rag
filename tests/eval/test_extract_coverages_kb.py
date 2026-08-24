"""KB 담보 catalog 정규화(순수 로직) 잠금 — 특약 제목 → 담보명.

KB 복합약관은 payout 표가 없어(가입금액 기저) 특약 제목(`###### N. 담보명`)을 담보 catalog
소스로 쓴다. 제목의 번호 접두·괄호·한 줄 병합된 갱신계약 변형을 벗겨 담보명만 남기는 게 핵심.
파일(clean.md) 읽는 경로는 자립 `make check`(golden_catalog)가 커버 — 여기선 순수 정규화만.
"""
from scripts.extract_coverages import _norm_kb_title


def test_strip_number_prefix_and_parens():
    """'N. 담보명(간편가입)' → 담보명."""
    assert _norm_kb_title("1. 장기요양간병비(1~5급)(간편가입)") == "장기요양간병비"
    assert _norm_kb_title("7. 질병입원일당(1일이상)(간편가입)") == "질병입원일당"


def test_strip_merged_renewal_variant():
    """한 줄에 병합된 2번째 변형/【갱신계약】은 컷(첫 담보명만)."""
    assert _norm_kb_title("15. 방문요양급여지원금(장기요양 1~5급, 월1회한)(간편가입) 15-1. 방문요양급여지원금(") == "방문요양급여지원금"
    assert _norm_kb_title("4. 간병인사용 상해입원일당【갱신계약】") == "간병인사용 상해입원일당"


def test_renewal_number_prefix():
    """'N-N. ' 갱신 번호 접두도 제거."""
    assert _norm_kb_title("8-1. 상해입원일당Ⅱ") == "상해입원일당Ⅱ"
