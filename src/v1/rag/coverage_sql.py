"""SQL 경로 — 별표3 ICD 보장판정. "이 병(코드) 보장돼요?".

약관은 병명이 아니라 KCD 코드 범위로 보장을 정의(암=C00~C97+특정D). 판정 기준은 코드. 담보
특정성(C=일반암 / D00~D09=제자리암 / D37~D48=경계성)과 제외 우선을 반영해 **3-값**(보장/미보장/
판정불가)을 낸다 — 판정불가가 precision-first(억지 판정 0). 미보장이면 실제 담보로 리다이렉트.

순수 로직(judge_coverage.py 이식) + 주입된 ranges({담보:[코드토큰]}, `CoverageRepository.get_ranges`).
병명→코드는 별도 계층 — 코드 직접 질의 우선 + 소형 병명맵(못 짚으면 None→RAG).
"""

from __future__ import annotations

import re

# 판정 게이트 — "보장돼요/보장되나요/보상되나"
_COVERAGE_INTENT_RE = re.compile(r"보장\s*(?:되|돼|받|하|해|여|가능|범위|여부)|보상\s*(?:되|돼|받)")

# 질의 담보 힌트(병명 키워드 → 담보). '암'은 넓어 마지막(제자리암·경계성 먼저 매칭).
_COVERAGE_HINT = [
    ("제자리암", "제자리암진단자금"),
    ("경계성종양", "경계성종양진단자금"),
    ("경계성", "경계성종양진단자금"),
    ("암", "암진단자금"),
]

# 병명 → 대표 KCD 코드(부위별 흔한 암 + 제자리/경계성). 코드 직접 질의가 우선이고 이건
# 편의 계층 — 전체 KCD 사전은 별도(미매핑 병명이면 None→RAG). 긴 병명이 먼저 매칭되게 정렬.
_DISEASE_CODE = [
    ("갑상선암", "C73"), ("자궁경부암", "C53"), ("자궁체부암", "C54"), ("전립선암", "C61"),
    ("다발골수종", "C90"), ("비인두암", "C11"), ("담도암", "C24"), ("식도암", "C15"),
    ("유방암", "C50"), ("위암", "C16"), ("폐암", "C34"), ("간암", "C22"),
    ("대장암", "C18"), ("직장암", "C20"), ("췌장암", "C25"), ("신장암", "C64"),
    ("방광암", "C67"), ("난소암", "C56"), ("후두암", "C32"), ("구강암", "C06"),
    ("뇌종양", "C71"), ("뇌암", "C71"), ("백혈병", "C95"), ("림프종", "C85"),
    ("흑색종", "C43"), ("피부암", "C44"), ("설암", "C02"), ("골육종", "C41"),
    ("제자리암", "D05"), ("상피내암", "D05"), ("경계성종양", "D41"),
    ("암", "C50"),   # 일반 '암'(부위 미지정) — 반드시 특정 병명 뒤(먼저 매칭 방지)
]

_CODE_RE = re.compile(r"([A-Z])(\d{2})(?:\.(\d))?")


def is_coverage_query(query: str) -> bool:
    """"이 병 보장돼요?" 류 보장판정 질의인가 — /answer의 coverage SQL 라우팅 게이트."""
    return bool(_COVERAGE_INTENT_RE.search(query))


def extract_code(query: str) -> str | None:
    """질의에서 ICD 코드(명시 우선) 또는 병명→대표코드. 못 짚으면 None(→RAG)."""
    m = _CODE_RE.search(query)
    if m:
        return f"{m.group(1)}{m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")
    for name, code in _DISEASE_CODE:
        if name in query:
            return code
    return None


def extract_coverage(query: str) -> str | None:
    """질의에서 담보 힌트(제자리암/경계성/암). 없으면 None(어느 담보든 매칭 탐색)."""
    for kw, cov in _COVERAGE_HINT:
        if kw in query:
            return cov
    return None


# ── 코드 튜플 판정 (judge_coverage.py 이식) ─────────────────────────────────
def _code(s: str):
    m = re.match(r"\s*([A-Z])(\d{1,2})(?:\.(\d))?", s)
    return (m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None) if m else None


def _bounds(tok: str):
    parts = re.split(r"\s*[~\-]\s*", tok)
    if len(parts) == 2:
        lo, hi = _code(parts[0]), _code(parts[1])
        return (lo[0], lo[1], lo[2] or 0), (hi[0], hi[1], hi[2] if hi[2] is not None else 9)
    c = _code(tok)
    if c[2] is None:
        return (c[0], c[1], 0), (c[0], c[1], 9)      # 카테고리 Cnn = Cnn.0~Cnn.9
    return c, c


def _contains(code: str, tok: str) -> bool:
    c = _code(code)
    if not c:
        return False
    c = (c[0], c[1], c[2] or 0)
    lo, hi = _bounds(tok)
    return lo <= c <= hi


def judge_coverage(code: str, ranges: dict, coverage: str | None = None) -> dict:
    """(코드, ranges={담보:[토큰]}, 담보?) → 3-값 판정 + 근거.

    coverage 지정: 그 담보 범위면 보장, 다른 담보면 미보장(+리다이렉트), 없으면 판정불가.
    coverage 미지정: 코드를 담은 담보를 찾아 보장(그 담보), 없으면 판정불가.
    """
    if coverage:
        for t in ranges.get(coverage, []):
            if _contains(code, t):
                return {"verdict": "보장", "coverage": coverage, "redirect_coverage": None,
                        "evidence": f"{code} ∈ {coverage} 범위 {t}"}
        for cov, toks in ranges.items():
            if cov == coverage:
                continue
            other = next((t for t in toks if _contains(code, t)), None)
            if other:
                return {"verdict": "미보장", "coverage": coverage, "redirect_coverage": cov,
                        "evidence": f"{code}는 {cov} 범위({other}) — {coverage} 아님"}
        return {"verdict": "판정불가", "coverage": coverage, "redirect_coverage": None,
                "evidence": f"{code} 어느 범위에도 없음 → RAG/전문가"}
    # 담보 미지정 — 코드를 담은 담보 탐색
    for cov, toks in ranges.items():
        hit = next((t for t in toks if _contains(code, t)), None)
        if hit:
            return {"verdict": "보장", "coverage": cov, "redirect_coverage": None,
                    "evidence": f"{code} ∈ {cov} 범위 {hit}"}
    return {"verdict": "판정불가", "coverage": None, "redirect_coverage": None,
            "evidence": f"{code} 어느 범위에도 없음 → RAG/전문가"}


def effective_coverage(verdict: dict | None) -> str | None:
    """판정 결과의 '실제 지급 담보' — reconcile용. 보장이면 그 담보, 미보장이면 리다이렉트,
    판정불가면 None. 이 담보의 payout을 붙이면 "얼마+보장"의 정합 답(모순 없음)이 된다."""
    if not verdict:
        return None
    if verdict["verdict"] == "보장":
        return verdict.get("coverage")
    if verdict["verdict"] == "미보장":
        return verdict.get("redirect_coverage")
    return None


def format_coverage(code: str | None, verdict: dict | None) -> str:
    """보장판정 결과를 소비자 답변 한 줄로. code 못 짚으면 RAG 폴백."""
    if not code:
        return "질의에서 질병코드를 특정하지 못했습니다(병명→코드는 RAG 소관 →RAG)."
    if not verdict:
        return f"{code}: 판정 근거(별표3)를 찾지 못했습니다(→RAG)."
    v = verdict["verdict"]
    if v == "보장":
        return f"{code} → 보장 ({verdict['coverage']}). {verdict['evidence']}"
    if v == "미보장":
        rd = f" → 실제 담보: {verdict['redirect_coverage']}" if verdict.get("redirect_coverage") else ""
        return f"{code} → 미보장 ({verdict['coverage']}){rd}. {verdict['evidence']}"
    return f"{code} → 판정불가 — 별표3 범위 밖. 전문가/RAG 확인 필요."
