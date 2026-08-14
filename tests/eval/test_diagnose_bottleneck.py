"""병목 판별식(diagnose_bottleneck) 순수 로직 — 파인튜닝 게이트를 좌우하는 결정.

diagnose(retr, gen, seg)는 retrieval 충분성 × generation 품질(+측정품질) × 세그먼트로
retrieval-bound / generation-bound / generation-leaning / healthy / 판정불가를 낸다
(로드맵 §1.4). 이 결정이 Phase 1(임베딩) vs Phase 2(LoRA) 착수를 가르므로, 로직 자체를
합성 입력으로 못박아 조용한 회귀를 막는다(측정·검증된 것만 메인 경로에).

핵심 계약:
  - 입력 품질(nan·편향 judge)을 스스로 채점 → 확정/잠정을 가름(precision-first).
  - retrieval 충분 판정은 recall@5(포화) 아닌 게 아니라, 세그먼트 비교는 미포화 축(@1·MRR).
"""
import math

from scripts.diagnose_bottleneck import diagnose
from scripts.eval_retrieval import _classify_segment, DOMAIN_VOCAB


def _retr(r5=1.0, r3=0.96, r1=0.76, mrr=0.861, n=25):
    return {"present": True, "recall": {1: r1, 3: r3, 5: r5, 10: 1.0}, "mrr": mrr, "n": n}


def _gen(faith=0.6, relev=0.7, biased=False, n=10):
    return {"present": True, "faithfulness": faith, "answer_relevancy": relev,
            "context_utilization": None, "n": n,
            "judge": "gpt-4o-mini" if not biased else "vllm (self-judge, biased)", "biased": biased}


def _seg(d1=0.84, dmrr=0.92, g1=0.50, gmrr=0.67):
    return {"present": True, "segments": {
        "domain": {"n": 19, "recall": {"1": d1, "5": 1.0}, "mrr": dmrr},
        "general": {"n": 6, "recall": {"1": g1, "5": 1.0}, "mrr": gmrr}}}


# ── 판정 분기 ────────────────────────────────────────────────────────────────
def test_retrieval_bound_when_recall_low():
    """정답 청크가 top-k에 충분히 안 들면 retrieval-bound → Phase 1."""
    v = diagnose(_retr(r5=0.6, r3=0.5), _gen())
    assert v["verdict"] == "retrieval-bound"
    assert "Phase 1" in v["next"]


def test_generation_bound_confirmed_with_unbiased_judge():
    """retrieval 충분 + 비편향 judge로 faithfulness 약함 측정됨 → generation-bound 확정 → Phase 2."""
    v = diagnose(_retr(), _gen(faith=0.55, relev=0.6, biased=False))
    assert v["verdict"] == "generation-bound"
    assert v["confidence"] == "확정"
    assert v["gate"] == "OPEN"
    assert "Phase 2" in v["next"]


def test_generation_leaning_when_faithfulness_nan():
    """현재 실제 상태 — retrieval 충분하나 faithfulness=nan → 잠정·게이트 BLOCKED."""
    v = diagnose(_retr(), _gen(faith=float("nan"), relev=0.695, biased=True))
    assert v["verdict"] == "generation-leaning"
    assert v["confidence"] == "잠정"
    assert v["gate"] == "BLOCKED"
    assert "judge" in v["next"]  # 비편향 judge 재측정 지시


def test_biased_judge_blocks_confirmation_even_if_faithfulness_present():
    """faithfulness 값이 있어도 self-judge(편향)면 확정 못 함 → 잠정."""
    v = diagnose(_retr(), _gen(faith=0.55, relev=0.6, biased=True))
    assert v["verdict"] == "generation-leaning"
    assert v["gate"] == "BLOCKED"


def test_healthy_when_retrieval_sufficient_and_generation_strong():
    """retrieval 충분 + generation 목표 충족(비편향) → 병목 없음, 트리거 미충족."""
    v = diagnose(_retr(), _gen(faith=0.9, relev=0.9, biased=False))
    assert v["verdict"] == "healthy"
    assert "미도입" in v["next"]


def test_undetermined_without_retrieval_baseline():
    v = diagnose({"present": False}, _gen())
    assert v["verdict"] == "판정불가"
    assert v["gate"] == "BLOCKED"


def test_recall3_sufficiency_path():
    """recall@5가 목표 미달이어도 recall@3가 충분하면 retrieval 충분으로 본다(둘 중 하나)."""
    v = diagnose(_retr(r5=0.9, r3=0.95), _gen(faith=0.9, relev=0.9, biased=False))
    assert v["verdict"] == "healthy"


# ── 세그먼트 반영(§1.4 핵심) ───────────────────────────────────────────────────
def test_segment_inversion_noted_domain_stronger():
    """도메인이 일반보다 우위(실측)면 note에 반증 + Phase1 기각 방향이 찍힌다."""
    v = diagnose(_retr(), _gen(faith=float("nan"), biased=True), _seg(d1=0.84, dmrr=0.92, g1=0.50, gmrr=0.67))
    joined = " ".join(v["notes"])
    assert "도메인" in joined
    assert "우위" in joined or "반증" in joined


def test_segment_domain_weak_flags_retrieval_signal():
    """도메인이 일반보다 recall@1·MRR 모두 유의미 열위면 retrieval-bound(도메인 특화) 신호."""
    v = diagnose(_retr(), _gen(faith=float("nan"), biased=True),
                 _seg(d1=0.4, dmrr=0.5, g1=0.9, gmrr=0.9))
    joined = " ".join(v["notes"])
    assert "Phase 1a" in joined or "retrieval-bound" in joined


def test_segment_absent_is_graceful():
    """세그먼트 파일 없어도 판정은 정상(회귀 방지)."""
    v = diagnose(_retr(), _gen(faith=float("nan"), biased=True), {"present": False})
    assert v["verdict"] == "generation-leaning"


# ── 세그먼트 분류기(공개 목록 기반, 재현 가능) ───────────────────────────────────
def test_classify_domain_vocab():
    assert _classify_segment("중환자실 입원급여금은 언제 지급되나요?") == "domain"
    assert _classify_segment("소득보장수술특약을 해지하면 해약환급금은?") == "domain"


def test_classify_general_when_no_domain_term():
    assert _classify_segment("보험금을 청구할 때 제출해야 하는 서류는 무엇인가요?") == "general"
    assert _classify_segment("보험금 청구 후 회사는 어떤 절차로 지급하나요?") == "general"


def test_domain_vocab_is_disclosed_list():
    """세그먼트가 재현·감사 가능하려면 목록이 코드에 공개돼 있어야 한다."""
    assert isinstance(DOMAIN_VOCAB, list) and len(DOMAIN_VOCAB) >= 10
    assert "중환자실" in DOMAIN_VOCAB and "특약" in DOMAIN_VOCAB
