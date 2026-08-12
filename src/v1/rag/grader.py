"""답변 검증 & 검색 품질 평가.

LLM 답변에서 구조적 참조(조항·별표·숫자)를 추출해 context와 대조하고 위험 등급을 부여한다.
의미 일치 검증(예: "보장된다" vs context의 "보장하지 아니한다")은 현재 미구현 —
결정론적 구조 검증 guardrail로 한정. 의미 검증 확장은 NLI classifier / LLM-as-judge 도입 고려.

구성:
  - 구조적 사실 추출 (조항 계층·별표·숫자+단위·날짜)
  - Claim 분해 (답변을 종결어미 기준 atomic 문장으로)
  - 근거 chunk 매핑 (어느 chunk가 어느 claim을 지지하는가)
  - 위험 등급 판정 (pass / warn / soft_fail / hard_fail)
  - CRAG 검색 품질 게이트 (rerank score threshold)

레퍼런스:
  - Self-RAG (Asai et al., ICLR 2024) — reflection token 기반 self-critique
  - FActScore (Min et al., EMNLP 2023) — atomic claim decomposition
  - AIS (Rashkin et al., Comp. Ling. 2023) — attribution test
  - CRAG (Yan et al., arXiv:2401.15884) — retrieval evaluator
  - Anthropic Citations API · Azure Groundedness Detection — 벤더 관행
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

from ..config.settings import CRAG_SCORE_THRESHOLD

__all__ = [
    "ArticleRef",
    "AppendixRef",
    "NumericFact",
    "Chunk",
    "RiskLevel",
    "FailureType",
    "extract_article_refs",
    "extract_appendix_refs",
    "extract_numeric_facts",
    "decompose_claims",
    "evaluate_retrieval",
    "verify_answer",
    "classify_failure",
    "build_hint",
]


# ==============================================================================
# 구조적 참조 타입 & 정규식
# ==============================================================================

# 계층형 조항 참조 (제N조 [제N항] [제N호] [제N목]).
# "조항은 맞고 항·호가 틀린" 사고 케이스까지 분리 검증하려면 계층 전부 보존.
_ARTICLE_HIERARCHY_RE = re.compile(
    r"제\s*(\d+)\s*조"
    r"(?:\s*제\s*(\d+)\s*항)?"
    r"(?:\s*제\s*(\d+)\s*호)?"
    r"(?:\s*제\s*(\d+)\s*목)?"
)

# 별표·부칙·서식·양식 — 조문과 독립된 구조적 참조.
_APPENDIX_RE = re.compile(
    r"별표\s*(\d+)"
    r"|부칙(?:\s*제\s*(\d+)\s*조)?"
    r"|서식\s*(\d+)"
    r"|양식\s*(\d+)"
)

# 단위 포함 숫자 span. 단위 정규화는 extract_numeric_facts에서.
_NUMBER_SPAN_RE = re.compile(
    r"(?P<amount>\d{1,3}(?:,\d{3})+|\d+)"
    r"\s*"
    r"(?P<unit>만\s*원|억\s*원|원|퍼센트|%|개월|일|년|세|살|회)?"
)
_DATE_RE = re.compile(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}")


@dataclass(frozen=True)
class ArticleRef:
    """계층형 조항 참조. article만 필수."""

    article: int
    paragraph: int | None = None
    subparagraph: int | None = None
    item: int | None = None

    def canonical(self) -> str:
        s = f"제{self.article}조"
        if self.paragraph:
            s += f" 제{self.paragraph}항"
        if self.subparagraph:
            s += f" 제{self.subparagraph}호"
        if self.item:
            s += f" 제{self.item}목"
        return s


@dataclass(frozen=True)
class AppendixRef:
    """별표·부칙·서식·양식 참조."""

    kind: Literal["별표", "부칙", "서식", "양식"]
    number: int | None = None

    def canonical(self) -> str:
        # 부칙은 한국어 문서에서 "부칙 제N조" 형태로 등장 (별표/서식/양식과 다름).
        # canonical을 자연 형식으로 유지해서 LLM hint·진단 출력 모두 자연스럽게.
        if self.number is None:
            return self.kind
        if self.kind == "부칙":
            return f"부칙 제{self.number}조"
        return f"{self.kind} {self.number}"


@dataclass(frozen=True)
class NumericFact:
    """단위 포함 canonical form — '1,000만원'과 '10,000,000원'이 동일 매칭되도록."""

    value: float
    unit: str  # KRW / PERCENT / DAYS / YEARS / AGE / COUNT / DATE
    original: str

    def canonical(self) -> str:
        return f"{self.value}:{self.unit}"


@dataclass
class Chunk:
    """검증 입력 — id와 content만. payload·score는 검증 로직에 불필요."""

    id: str
    content: str


RiskLevel = Literal["hard_fail", "soft_fail", "warn", "pass"]


# ==============================================================================
# 사실 추출
# ==============================================================================

def extract_article_refs(text: str) -> list[ArticleRef]:
    """텍스트에서 조항 참조(계층 포함)를 모두 추출."""
    return [
        ArticleRef(
            article=int(m.group(1)),
            paragraph=int(m.group(2)) if m.group(2) else None,
            subparagraph=int(m.group(3)) if m.group(3) else None,
            item=int(m.group(4)) if m.group(4) else None,
        )
        for m in _ARTICLE_HIERARCHY_RE.finditer(text)
    ]


def extract_appendix_refs(text: str) -> list[AppendixRef]:
    """텍스트에서 별표·부칙·서식·양식 참조를 모두 추출."""
    out: list[AppendixRef] = []
    for m in _APPENDIX_RE.finditer(text):
        if m.group(1):
            out.append(AppendixRef("별표", int(m.group(1))))
        elif m.group(0).startswith("부칙"):
            out.append(AppendixRef("부칙", int(m.group(2)) if m.group(2) else None))
        elif m.group(3):
            out.append(AppendixRef("서식", int(m.group(3))))
        elif m.group(4):
            out.append(AppendixRef("양식", int(m.group(4))))
    return out


# 단위 → (배율, canonical code). 개월→일 환산으로 '3개월'과 '90일'이 매칭되도록.
_NUMERIC_UNIT_MAP: dict[str, tuple[float, str]] = {
    "만원": (10_000, "KRW"),
    "억원": (100_000_000, "KRW"),
    "원": (1, "KRW"),
    "%": (1, "PERCENT"),
    "퍼센트": (1, "PERCENT"),
    "일": (1, "DAYS"),
    "개월": (30, "DAYS"),
    "년": (1, "YEARS"),
    "세": (1, "AGE"),
    "살": (1, "AGE"),
    "회": (1, "COUNT"),
}


def extract_numeric_facts(text: str) -> list[NumericFact]:
    """단위 포함 숫자를 canonical form으로 추출.

    단위 없는 raw 숫자는 조항번호 파싱과 겹쳐 false positive가 폭발하므로 제외.
    """
    out: list[NumericFact] = []

    for m in _NUMBER_SPAN_RE.finditer(text):
        raw = m.group("amount")
        unit = (m.group("unit") or "").replace(" ", "")
        if not unit:
            continue
        spec = _NUMERIC_UNIT_MAP.get(unit)
        if spec is None:
            continue
        multiplier, canonical_unit = spec
        out.append(NumericFact(float(raw.replace(",", "")) * multiplier, canonical_unit, m.group(0)))

    for m in _DATE_RE.finditer(text):
        normalized = re.sub(r"[.\/]", "-", m.group(0))
        out.append(NumericFact(0, "DATE", normalized))

    return out


# ==============================================================================
# Claim 분해 & 근거 chunk 매핑
# ==============================================================================

# 한국어 종결어미(다/요/음) 또는 문장부호로 분리. 복잡한 복문은 정확도 떨어짐 —
# 확장 시 LLM 기반 분해 (FActScore 스타일) 고려.
_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?다요음])\s+|\n+")


def decompose_claims(answer: str) -> list[str]:
    """답변을 atomic claim 단위로 분해."""
    return [c.strip() for c in _CLAIM_SPLIT_RE.split(answer) if c.strip()]


def _provenance_map(
    refs_canonical: set[str],
    chunks: list[Chunk],
    extract_fn: Callable[[str], list],
) -> dict[str, list[str]]:
    """각 참조가 어느 chunk에서 지지되는지 매핑.

    답변·context 양쪽에 모두 등장한 참조에 대해서만 호출하는 것이 호출자 책임.
    """
    out: dict[str, list[str]] = {r: [] for r in refs_canonical}
    for chunk in chunks:
        chunk_refs = {r.canonical() for r in extract_fn(chunk.content)}
        for r in refs_canonical & chunk_refs:
            out[r].append(chunk.id)
    return out


# ==============================================================================
# 위험 등급 판정
# ==============================================================================

# 비즈니스 영향 있는 단위 — 이 단위의 mismatch는 soft_fail, 나머지(AGE/COUNT)는 warn.
_BUSINESS_CRITICAL_UNITS = frozenset({"KRW", "DAYS", "YEARS", "PERCENT"})


# ==============================================================================
# Failure type classification — verify_answer 출력을 action으로 매핑
# ==============================================================================

FailureType = Literal[
    "generation_error",    # missing_ref가 context 인접 참조와 근접 → regenerate with hint
    "retrieval_gap",       # missing_ref가 context 어디에도 없음 → regenerate 금지
    "unit_error",          # 수치 mismatch가 같은 단위 내 근접값 → regenerate with hint
    "semantic_mismatch",   # 답변 의미와 context 반대 → regenerate 금지
    "minor",               # 사소한 차이 또는 이상 없음 → pass
]


# canonical 문자열("제43조", "제43조 제2항", "별표 1" 등)에서 숫자 파싱
_ARTICLE_NUM_RE = re.compile(r"제\s*(\d+)\s*조")
_PARAGRAPH_NUM_RE = re.compile(r"제\s*(\d+)\s*항")


def _article_num(canonical: str) -> int | None:
    m = _ARTICLE_NUM_RE.search(canonical)
    return int(m.group(1)) if m else None


def _paragraph_num(canonical: str) -> int | None:
    m = _PARAGRAPH_NUM_RE.search(canonical)
    return int(m.group(1)) if m else None


def _has_close_numeric(
    mismatch: dict,
    ctx_numerics: list[NumericFact],
    proximity_threshold: float = 0.5,
) -> bool:
    """답변의 수치 mismatch가 context 수치 중 하나와 같은 단위·근접 값이면 True.

    proximity = |ans - ctx| / max(|ans|, |ctx|) < threshold.
    예: 1,200만원 vs 1,000만원 → 0.166 < 0.5 → True (수치 착각 가능성).
        5억원 vs 100만원 → 0.998 > 0.5 → False (전혀 다른 수치, regenerate 무의미).
    """
    m_unit = mismatch.get("unit")
    m_value = float(mismatch.get("value", 0))
    if not m_unit or m_value == 0:
        return False
    for n in ctx_numerics:
        if n.unit != m_unit:
            continue
        denom = max(abs(m_value), abs(n.value))
        if denom == 0:
            continue
        if abs(m_value - n.value) / denom < proximity_threshold:
            return True
    return False


def _has_adjacent_article(missing: str, ctx_refs: set[str]) -> bool:
    """missing 참조가 ctx_refs 중 하나와 "인접 착각" 관계인지.

    판정 기준:
      - 조 번호가 ±1 이내 (제42조 vs 제43조)
      - 조 번호 같고 항 번호가 ±1 이내 (제43조 제1항 vs 제43조 제2항)
      - 조 번호 같고 한쪽만 계층 (제43조 vs 제43조 제1항)

    True면 generation_error (LLM이 근접한 번호를 착각한 케이스) → regenerate with hint 유효.
    """
    m_num = _article_num(missing)
    if m_num is None:
        return False
    for c in ctx_refs:
        c_num = _article_num(c)
        if c_num is None or abs(c_num - m_num) > 1:
            continue
        if c_num != m_num:
            return True  # 조 번호 ±1
        # 조가 같은 경우 — 항 번호 대조
        m_p = _paragraph_num(missing)
        c_p = _paragraph_num(c)
        if m_p is not None and c_p is not None and abs(c_p - m_p) <= 1:
            return True  # 인접 항
        if (m_p is None) != (c_p is None):
            return True  # 한쪽만 계층 정보 (ex: 제43조 vs 제43조 제1항)
    return False


def classify_failure(
    verification: dict,
    context: str,
    *,
    answer: str | None = None,
    semantic_judge: Callable[[str, str], bool] | None = None,
) -> FailureType:
    """verify_answer 출력을 5가지 action class로 분류.

    결정론적 규칙(조항 인접성·수치 근접성·비크리티컬 단위)으로 먼저 판정하고,
    semantic_judge가 주입됐을 때만 의미 반전 판정 수행. 기본은 regex+집합 비교만.
    """
    missing_refs: list[str] = verification.get("missing_refs", [])
    numeric_mismatches: list[dict] = verification.get("numeric_mismatches", [])

    if missing_refs:
        ctx_refs = {r.canonical() for r in extract_article_refs(context)} | {
            r.canonical() for r in extract_appendix_refs(context)
        }
        if any(_has_adjacent_article(m, ctx_refs) for m in missing_refs):
            return "generation_error"
        # 인접 참조가 전혀 없으면 retrieval이 근본적으로 부족 —
        # regenerate로 같은 실수를 재포장만 할 뿐. 재검색·clarify로 우회.
        return "retrieval_gap"

    if numeric_mismatches:
        # 비즈니스 크리티컬 단위(KRW/DAYS/YEARS/PERCENT)만 unit_error 후보 —
        # AGE·COUNT 같은 비크리티컬 단위는 regenerate가 사업적 가치 없음 (minor로 흡수).
        critical_mismatches = [
            n for n in numeric_mismatches
            if n.get("unit") in _BUSINESS_CRITICAL_UNITS
        ]
        if critical_mismatches:
            ctx_numerics = extract_numeric_facts(context)
            if any(_has_close_numeric(n, ctx_numerics) for n in critical_mismatches):
                return "unit_error"
        # 비크리티컬만 있거나, 크리티컬도 멀어 fall through → minor

    # 의미 검증 — 외부 judge(NLI / LLM judge 등) 주입 슬롯.
    # 기본 비활성(semantic_judge=None) — 한국어 보험 도메인에서 precision ≥ 0.9가 검증된
    # 후보 모델이 없는 상태에서 메인 경로에 끼우면 false positive로 hard_fail rate 폭증해
    # 시스템 신뢰도를 깎음. 도메인 평가셋 확보 + 후보 측정 후에만 채택 (README "검증되지 않은
    # 영역" 섹션 참조).
    if semantic_judge is not None and answer is not None:
        if semantic_judge(answer, context):
            return "semantic_mismatch"

    return "minor"


def build_hint(
    failure_type: FailureType,
    verification: dict,
    context: str,
) -> str:
    """Regenerate 시 LLM에게 전달할 외부 피드백 텍스트.

    Huang et al. ICLR 2024가 self-correction 성립 조건으로 지목한 "외부 피드백"의 구체화.
    blind regenerate(같은 실수 반복)를 막기 위해 verifier가 찾아낸 구조적 오류를
    "허용 목록 + 사용 금지 목록" 형태로 LLM에게 강제한다.

    failure_type별:
      - generation_error: context의 허용 참조 + 이전 답변에서 환각이었던 참조 노출
      - unit_error:       context의 허용 수치 + 이전 답변의 잘못된 수치 노출
      - retrieval_gap / semantic_mismatch / minor: regenerate 금지/불필요 → 빈 문자열
    """
    if failure_type == "generation_error":
        allowed_refs = sorted(
            {r.canonical() for r in extract_article_refs(context)}
            | {r.canonical() for r in extract_appendix_refs(context)}
        )
        missing = verification.get("missing_refs", [])
        lines = ["아래 허용 참조 목록만 사용하여 답변을 다시 작성하세요."]
        if allowed_refs:
            lines.append(f"허용 참조: {', '.join(allowed_refs)}")
        if missing:
            lines.append(
                f"사용 금지 (이전 답변에서 context에 없던 참조): {', '.join(missing)}"
            )
        return "\n".join(lines)

    if failure_type == "unit_error":
        # 허용 수치 = context의 numeric_facts (original 형태 — 사람이 읽는 텍스트)
        ctx_numerics = extract_numeric_facts(context)
        allowed = sorted({n.original for n in ctx_numerics if n.original})
        # 금지 수치 = 이전 답변에서 context에 없던 수치 (verification.numeric_mismatches의 original)
        mismatched = [
            n.get("original", "")
            for n in verification.get("numeric_mismatches", [])
            if n.get("original")
        ]
        lines = ["아래 허용 수치 목록만 사용하여 답변을 다시 작성하세요."]
        if allowed:
            lines.append(f"허용 수치: {', '.join(allowed)}")
        if mismatched:
            lines.append(
                f"사용 금지 (이전 답변에서 context에 없던 수치): {', '.join(mismatched)}"
            )
        return "\n".join(lines)

    return ""  # retrieval_gap / semantic_mismatch / minor — regenerate 안 함


def _decide_risk(
    missing_articles: list[str],
    missing_appendices: list[str],
    numeric_mismatches: list[NumericFact],
) -> RiskLevel:
    """
    hard_fail: 존재하지 않는 조항/별표/부칙 참조 (치명적 환각).
    soft_fail: 금액·기간·비율 mismatch (비즈니스 영향).
    warn:      사소한 숫자 차이 (나이·횟수 등).
    pass:      모두 일치.
    """
    if missing_articles or missing_appendices:
        return "hard_fail"
    if numeric_mismatches:
        if any(n.unit in _BUSINESS_CRITICAL_UNITS for n in numeric_mismatches):
            return "soft_fail"
        return "warn"
    return "pass"


# ==============================================================================
# CRAG — 검색 품질 게이트
# ==============================================================================

def evaluate_retrieval(
    ranked: list,
    threshold: float = CRAG_SCORE_THRESHOLD,
) -> bool:
    """CrossEncoder rerank score 기반 검색 품질 평가.

    True  → 충분.
    False → 부족, 쿼리 재작성 후 재검색 트리거.
    """
    if not ranked:
        return False
    return float(ranked[0][1]) >= threshold


# ==============================================================================
# 답변 검증 — 오케스트레이션
# ==============================================================================

def _build_claim_record(
    claim_text: str,
    article_provenance: dict[str, list[str]],
    appendix_provenance: dict[str, list[str]],
) -> dict:
    """답변의 한 claim에 대해 추출된 사실 + 근거 chunk id 기록."""
    article_refs = [r.canonical() for r in extract_article_refs(claim_text)]
    appendix_refs = [r.canonical() for r in extract_appendix_refs(claim_text)]
    numeric_facts = extract_numeric_facts(claim_text)

    supported_chunks = sorted(
        {cid for r in article_refs for cid in article_provenance.get(r, [])}
        | {cid for r in appendix_refs for cid in appendix_provenance.get(r, [])}
    )

    return {
        "text": claim_text,
        "extracted_refs": article_refs + appendix_refs,
        "extracted_numerics": [n.canonical() for n in numeric_facts],
        "supported_by_chunks": supported_chunks,
    }


def _build_warnings(
    missing_articles: list[str],
    missing_appendices: list[str],
    numeric_mismatches: list[NumericFact],
) -> list[str]:
    """응답의 warnings 필드 — 사람이 읽는 경고 메시지.

    문구 주의: 이 플래그는 '환각'이 아니라 '인용 참조가 검색 상위(context)에 없음'을 뜻한다.
    실측(design-retrospective §1.5) hard_fail 6/6이 오탐이었고 전부 검색 격차(문서엔 있는데
    top-k에 안 담김)였으므로, 환각을 단정하지 않는 표현을 쓴다. 스코프 3-state 재설계는
    docs/verification-redesign.md 참조(트리거 시 구현).
    """
    warnings: list[str] = []
    if missing_articles:
        warnings.append(f"인용 조항이 검색 근거에 없음(검색 격차 가능): {', '.join(missing_articles)}")
    if missing_appendices:
        warnings.append(f"인용 별표/부칙/서식이 검색 근거에 없음(검색 격차 가능): {', '.join(missing_appendices)}")
    if numeric_mismatches:
        samples = ", ".join(n.original for n in numeric_mismatches[:5])
        warnings.append(f"인용 숫자/기간/금액이 검색 근거와 불일치/미확인: {samples}")
    return warnings


def verify_answer(
    answer: str,
    context: str | None = None,
    chunks: list[Chunk] | None = None,
) -> dict:
    """LLM 답변의 구조적 검증 + 근거 chunk 매핑 + 위험 등급 판정.

    Args:
        answer:  LLM이 생성한 답변.
        context: 단일 문자열 context (chunks 미지정 시 폴백).
        chunks:  chunk 단위 provenance 매핑용. 없으면 context 전체를 단일 chunk로 취급.

    Returns:
        dict with keys:
          - risk_level: pass / warn / soft_fail / hard_fail
          - claims: [{text, extracted_refs, extracted_numerics, supported_by_chunks}]
          - missing_refs: 답변에만 있고 context에 없는 구조적 참조
          - numeric_mismatches: 답변에만 있고 context에 없는 수치 사실
          - warnings: 응답 노출용 사람이 읽는 경고 메시지
    """
    if chunks is None:
        ctx_text = context or ""
        chunks = [Chunk(id="context", content=ctx_text)] if ctx_text else []

    # 근거 범위 = LLM이 실제로 조건화한 것. chunks(개별 청크 provenance용)에 더해
    # sibling 확장된 context(LLM이 본 전체)까지 포함해야 한다 — 인용 조가 top-k 청크가 아닌
    # 인접 sibling(예: 제4조)에서 왔을 때, chunks만 보면 "근거에 없음"으로 오판(hard_fail).
    ctx_parts = [c.content for c in chunks]
    if context:
        ctx_parts.append(context)
    ctx_joined = "\n".join(ctx_parts)

    ans_articles = extract_article_refs(answer)
    ans_appendices_set = {r.canonical() for r in extract_appendix_refs(answer)}
    ans_numerics = extract_numeric_facts(answer)
    ctx_articles = extract_article_refs(ctx_joined)
    ctx_appendices_set = {r.canonical() for r in extract_appendix_refs(ctx_joined)}
    ctx_numerics_set = {n.canonical() for n in extract_numeric_facts(ctx_joined)}

    # 조(條) 단위 매칭 — 항/호/목 입도로 대조하면 오탐이 폭발한다. context 본문의 항은
    # "1"·"2" 맨숫자로 쓰여 "제N항"으로 추출되지 않는데(heading_path도 조까지만 표기),
    # LLM 답변은 "제10조 제2항"으로 격식화한다. 조가 근거에 있으면 그 조 청크 안에 항 세부가
    # 담기므로 조 존재만 확인한다 — pipeline.md §4 "검증기 정밀화 = 조 단위". 수치 오류는
    # numeric_mismatches가 별도로 잡으므로 실질 검증력은 유지된다.
    ctx_article_nums = {r.article for r in ctx_articles}
    missing_articles = sorted(
        {r.canonical() for r in ans_articles if r.article not in ctx_article_nums}
    )
    missing_appendices = sorted(ans_appendices_set - ctx_appendices_set)
    numeric_mismatches = [n for n in ans_numerics if n.canonical() not in ctx_numerics_set]

    # 근거 chunk 매핑 — 조가 근거에 있는 답변 참조를, 그 조를 담은 chunk에 조 번호로 매핑
    # (항 입도 무시, 위와 동일 사유. "제10조 제2항" → 제10조를 담은 chunk의 id).
    article_provenance: dict[str, list[str]] = {}
    for r in ans_articles:
        if r.article not in ctx_article_nums:
            continue
        ids = [
            c.id for c in chunks
            if r.article in {cr.article for cr in extract_article_refs(c.content)}
        ]
        if ids:
            article_provenance[r.canonical()] = ids
    appendix_provenance = _provenance_map(
        ans_appendices_set & ctx_appendices_set, chunks, extract_appendix_refs
    )

    claims = [
        _build_claim_record(c, article_provenance, appendix_provenance)
        for c in decompose_claims(answer)
    ]

    return {
        "risk_level": _decide_risk(missing_articles, missing_appendices, numeric_mismatches),
        "claims": claims,
        "missing_refs": missing_articles + missing_appendices,
        "numeric_mismatches": [
            {"value": n.value, "unit": n.unit, "original": n.original}
            for n in numeric_mismatches
        ],
        "warnings": _build_warnings(missing_articles, missing_appendices, numeric_mismatches),
    }
