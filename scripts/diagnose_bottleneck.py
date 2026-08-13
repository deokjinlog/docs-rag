"""병목 분해 — retrieval-bound vs generation-bound 판별식 (로드맵 Phase 0 게이트).

파인튜닝은 비싸고 되돌리기 어렵다. **어느 쪽이 병목인지 데이터로 모른 채** 임베딩 대조학습(Phase 1)
이나 LoRA(Phase 2)에 손대면 dead infrastructure다. 이 스크립트가 [roadmap.md](../docs/roadmap.md)
§1.4의 판별식 — 표에 "병목 분해 | 없음"으로 비어 있던 자리 — 을 실행 가능하게 만든다.

입력은 이미 측정된 두 산출물뿐(새 스택 불필요):
  - retrieval 신호: data/eval/retrieval_baseline.json  (recall@k · MRR, eval_retrieval.py)
  - generation 신호: data/eval/ragas_eval_result.json  (faithfulness · answer_relevancy, eval_ragas.py)

핵심은 **입력 품질을 스스로 채점**하는 것 — RAGAS가 self-judge(편향)거나 지표가 nan이면 판정을
"확정"이 아니라 "잠정"으로 낮추고 무엇을 제대로 재야 게이트가 열리는지 명시한다(precision-first:
확신에 찬 오판보다 "아직 모른다"). 판정 결과는 data/eval/bottleneck_verdict.json 에 보존.

용법: python3 scripts/diagnose_bottleneck.py    (make diagnose)
"""
import os
import json
import math

HERE = os.path.dirname(__file__)
EVAL = os.path.join(HERE, "..", "data", "eval")

# 판정 임계 — 근거는 retrieval_baseline 주석(recall@5=1.0·@3=0.96을 '충분'으로 취급)과
# RAGAS 통상 'good' 기준(≥0.8). 임계는 여기 한 곳에서만 바꾼다.
RECALL5_TARGET = 0.95      # 정답 청크가 top-5(=LLM 컨텍스트)에 드는 비율 목표
RECALL3_TARGET = 0.90      # /answer 기본 top_k=3 이 실제 보는 범위
FAITHFULNESS_TARGET = 0.80
RELEVANCY_TARGET = 0.80


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))


def load_retrieval() -> dict:
    p = os.path.join(EVAL, "retrieval_baseline.json")
    if not os.path.exists(p):
        return {"present": False}
    d = json.load(open(p, encoding="utf-8"))
    rec = {int(k): v for k, v in d.get("recall", {}).items()}
    return {"present": True, "recall": rec, "mrr": d.get("mrr"), "n": d.get("n")}


def load_segments() -> dict:
    """retrieval 세그먼트 분해(eval_retrieval --segment) — 도메인 어휘 vs 일반 recall.
    §1.4: 도메인 쿼리가 일반보다 유의미 열위면 retrieval-bound(도메인 특화) 신호."""
    p = os.path.join(EVAL, "retrieval_segments.json")
    if not os.path.exists(p):
        return {"present": False}
    d = json.load(open(p, encoding="utf-8"))
    return {"present": True, **d}


def load_generation() -> dict:
    p = os.path.join(EVAL, "ragas_eval_result.json")
    if not os.path.exists(p):
        return {"present": False}
    d = json.load(open(p, encoding="utf-8"))
    m = d.get("metrics", {})
    judge = d.get("judge_llm", "")
    return {
        "present": True,
        "faithfulness": m.get("faithfulness"),
        "answer_relevancy": m.get("answer_relevancy"),
        "context_utilization": m.get("context_utilization"),
        "n": d.get("question_count"),
        "judge": judge,
        # self-judge(serving=judge)면 self-preference bias → 신뢰 강등
        "biased": bool(d.get("self_preference_bias_risk")) or "self-judge" in judge,
    }


def diagnose(retr: dict, gen: dict, seg: dict | None = None) -> dict:
    """§1.4 판별식 — retrieval 충분성 × generation 품질(+측정품질) → 병목 방향."""
    notes = []
    seg = seg or {"present": False}

    # ── retrieval 충분성: 정답 청크가 top-k에 드는가 ─────────────────────────
    if not retr.get("present"):
        return {"verdict": "판정불가", "reason": "retrieval_baseline.json 없음 → eval_retrieval 먼저",
                "gate": "BLOCKED", "next": None}
    r5 = retr["recall"].get(5)
    r3 = retr["recall"].get(3)
    retrieval_sufficient = (r5 is not None and r5 >= RECALL5_TARGET) or \
                           (r3 is not None and r3 >= RECALL3_TARGET)
    notes.append(f"retrieval: recall@5={r5} recall@3={r3} MRR={retr.get('mrr')} (n={retr.get('n')}) "
                 f"→ {'충분' if retrieval_sufficient else '부족'}")

    # ── 세그먼트 분해: 도메인 어휘 쿼리가 일반보다 열위인가(§1.4 핵심) ─────────
    # recall@5는 집계 1.0이면 세그먼트도 정의상 1.0(포화) → 판별 신호는 미포화 축(recall@1·MRR).
    domain_weak = False
    if seg.get("present"):
        s = seg.get("segments", {})
        dom, gen_s = s.get("domain", {}), s.get("general", {})
        d1, g1 = dom.get("recall", {}).get("1"), gen_s.get("recall", {}).get("1")
        dm, gm = dom.get("mrr"), gen_s.get("mrr")
        if d1 is not None and g1 is not None:
            # 도메인 열위 = 도메인이 일반보다 recall@1·MRR 모두 유의미(≥0.1) 낮을 때
            domain_weak = (g1 - d1 >= 0.1) and (gm is not None and dm is not None and gm - dm >= 0.1)
            inverted = (d1 - g1 >= 0.1) or (dm is not None and gm is not None and dm - gm >= 0.1)
            notes.append(f"  세그먼트(n=도메인{dom.get('n')}/일반{gen_s.get('n')}): "
                         f"recall@1 도메인={d1:.3f} vs 일반={g1:.3f} · MRR 도메인={dm} vs 일반={gm}")
            if domain_weak:
                notes.append("    → 도메인 어휘 쿼리 열위 → retrieval-bound(도메인 특화) 신호 → Phase 1a(리랭커) 검토")
            elif inverted:
                notes.append("    → 도메인이 오히려 우위(변별력 있는 약관 용어가 강한 앵커) → "
                             "retrieval-bound 반증, Phase 1(도메인 임베딩) 가설 기각 방향")
            else:
                notes.append("    → 세그먼트 차이 미미 → retrieval 병목 배제 견고화")

    # ── generation 품질 + 측정품질 ────────────────────────────────────────
    gen_measured = False
    gen_weak = None
    if gen.get("present"):
        faith = gen.get("faithfulness")
        relev = gen.get("answer_relevancy")
        faith_ok = _is_num(faith)
        relev_ok = _is_num(relev)
        # 측정 '됐다'의 조건: 핵심 지표 non-nan + judge 비편향
        gen_measured = (faith_ok or relev_ok) and not gen.get("biased")
        weak_signals = []
        if faith_ok and faith < FAITHFULNESS_TARGET:
            weak_signals.append(f"faithfulness={round(faith,3)}<{FAITHFULNESS_TARGET}")
        if relev_ok and relev < RELEVANCY_TARGET:
            weak_signals.append(f"answer_relevancy={round(relev,3)}<{RELEVANCY_TARGET}")
        gen_weak = bool(weak_signals)
        notes.append(f"generation: faithfulness={faith} answer_relevancy={relev} "
                     f"judge={'편향(self)' if gen.get('biased') else '분리'} (n={gen.get('n')})"
                     + (f" → 약함 신호: {', '.join(weak_signals)}" if weak_signals else ""))
        if not faith_ok:
            notes.append("  ⚠ faithfulness=nan → 근거준수 미측정(생성측 핵심 신호 공백)")
        if gen.get("biased"):
            notes.append("  ⚠ judge=self(serving과 동일) → self-preference bias, 값 신뢰 강등")
    else:
        notes.append("generation: ragas_eval_result.json 없음")

    # ── 판정 (§1.4) ──────────────────────────────────────────────────────
    if not retrieval_sufficient:
        return {"verdict": "retrieval-bound", "confidence": "잠정",
                "reason": "정답 청크가 top-k에 충분히 안 듦 → 검색이 병목",
                "gate": "OPEN(조건부)", "next": "Phase 1 (임베딩 대조학습) — 단, 도메인용어 쿼리 세분 recall 확인 후",
                "notes": notes}

    # retrieval 충분 → 병목은 생성 쪽으로 기운다(정답은 컨텍스트에 있음)
    if gen_measured and gen_weak:
        return {"verdict": "generation-bound", "confidence": "확정",
                "reason": "정답 청크는 top-k에 드는데(검색 충분) 근거준수/관련성이 목표 미달 → 생성이 병목",
                "gate": "OPEN", "next": "Phase 2 (Qwen3 LoRA 도메인 어댑터)",
                "notes": notes}
    if gen_weak:  # 약함 신호는 있으나 측정품질 미달(nan·편향)
        return {"verdict": "generation-leaning", "confidence": "잠정",
                "reason": "retrieval 충분(recall@5≥목표)이라 검색 병목은 배제되나, 생성측 지표가 "
                          "nan/편향-judge라 확정 불가. 방향은 generation이지만 게이트는 아직 닫힘",
                "gate": "BLOCKED",
                "next": "eval_ragas를 **비편향 judge(OPENAI_API_KEY, GPT-4o-mini)**로 재측정해 "
                        "faithfulness 확정 → 그 값이 목표 미달이면 Phase 2 개시",
                "notes": notes}
    if gen_measured and not gen_weak:
        return {"verdict": "healthy", "confidence": "확정",
                "reason": "retrieval 충분 + generation 목표 충족 → 현 병목 없음(파인튜닝 트리거 미충족)",
                "gate": "닫힘(정상)", "next": "미도입(default) — 운영 trace로 실사용 분포 관측 지속",
                "notes": notes}
    return {"verdict": "판정불가", "confidence": "—",
            "reason": "retrieval는 충분하나 generation 신호가 없음/미측정 → 병목 방향 미상",
            "gate": "BLOCKED",
            "next": "eval_ragas를 비편향 judge로 측정(faithfulness·answer_relevancy)",
            "notes": notes}


def main():
    retr = load_retrieval()
    gen = load_generation()
    seg = load_segments()
    v = diagnose(retr, gen, seg)

    print("=" * 70)
    print("병목 분해 — retrieval-bound vs generation-bound (로드맵 Phase 0 게이트)")
    print("=" * 70)
    for line in v.get("notes", []):
        print("  " + line)
    print("-" * 70)
    print(f"  판정      : {v['verdict']}  ({v.get('confidence','')})")
    print(f"  근거      : {v['reason']}")
    print(f"  게이트    : {v['gate']}")
    print(f"  다음 단계 : {v['next']}")
    print("=" * 70)

    out = os.path.join(EVAL, "bottleneck_verdict.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"retrieval": retr, "segments": seg, "generation": gen, "diagnosis": v,
                   "targets": {"recall@5": RECALL5_TARGET, "recall@3": RECALL3_TARGET,
                               "faithfulness": FAITHFULNESS_TARGET, "answer_relevancy": RELEVANCY_TARGET}},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"→ 저장: data/eval/bottleneck_verdict.json")


if __name__ == "__main__":
    main()
