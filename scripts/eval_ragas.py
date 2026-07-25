"""RAGAS 평가 스크립트.
RAG 파이프라인 품질을 Faithfulness, Answer Relevancy, Context Utilization으로 측정.
골든셋 없이 실행 가능 — question + context + answer만 사용.

Judge LLM 분리:
    Serving은 vLLM/Qwen3, Judge는 GPT-4o-mini로 분리 (self-preference bias 회피).
    OPENAI_API_KEY 미설정 시 vLLM judge로 fallback (점수에 "biased" 라벨 부착).
    근거: Zheng et al. NeurIPS 2023 (MT-Bench).

사용법:
    OPENAI_API_KEY=sk-... uv run python scripts/eval_ragas.py
    uv run python scripts/eval_ragas.py --basic    # RAGAS 없이 기본 통계만
    RAGAS_JUDGE_MODEL=gpt-4o uv run python scripts/eval_ragas.py  # judge 모델 변경
"""

import argparse
import json
import os
import re
from datetime import datetime

import requests

API_URL = os.environ.get("API_URL", "http://localhost:8002/api/v1/docs-rag")


def _clean_answer(answer: str) -> str:
    """think 태그 제거."""
    return re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()


# 문서별로 document_id를 지정해 cross-document 혼입 방지.
# document_id는 운영 DB 기준 — 자체 문서로 교체 시 아래 매핑만 갱신.
#   0010 = 자녀보험 약관 (KB 희망플러스자녀보험Ⅱ 21.04 구버전)
#   0011 = 자녀보험 약관 (KB 희망플러스자녀보험Ⅱ 21.07 최신) ← 평가셋 사용
#   0012 = 건강보험 약관 (KB 골든라이프케어 간편건강보험 26.01) ← 평가셋 사용
#   0013 = 운전자상해보험 (KB 플러스 운전자상해보험 26.01)     ← 평가셋 사용
#   0014 = 건강보험 약관 (KB_간편건강보험)
#   0015 = 치아보험 (New치아보험)
#   0016 = 입원비보험 (다이렉트늘안심입원비보험)
EVAL_QUESTIONS = [
    # ── 운전자상해보험 → 0013 (KB 플러스 운전자상해보험 26.01) ──
    {"query": "제43조 무면허운전 등의 금지", "service_code": "01", "document_id": "0013", "expected_type": "structured_lookup"},
    {"query": "제7조 보험금을 지급하지 않는 사유", "service_code": "01", "document_id": "0013", "expected_type": "structured_lookup"},
    {"query": "무면허운전 시 보험금 지급이 되나요?", "service_code": "01", "document_id": "0013", "expected_type": "interpretation"},
    {"query": "음주운전 사고도 보험금 지급 대상인가요?", "service_code": "01", "document_id": "0013", "expected_type": "interpretation"},
    {"query": "고의로 사고를 낸 경우에도 보장이 되나요?", "service_code": "01", "document_id": "0013", "expected_type": "interpretation"},
    {"query": "보험금 청구 절차가 어떻게 되나요?", "service_code": "01", "document_id": "0013", "expected_type": "procedure"},
    {"query": "계약을 해지하려면 어떻게 해야 하나요?", "service_code": "01", "document_id": "0013", "expected_type": "procedure"},
    {"query": "영업용과 자가용 운전자의 보장 차이는?", "service_code": "01", "document_id": "0013", "expected_type": "comparison"},
    {"query": "운전면허정지 보장금은 최대 며칠까지인가요?", "service_code": "01", "document_id": "0013", "expected_type": "simple_fact"},
    {"query": "가지급보험금은 얼마까지 받을 수 있나요?", "service_code": "01", "document_id": "0013", "expected_type": "simple_fact"},

    # ── 자녀보험 → 0011 (KB 희망플러스자녀보험Ⅱ 21.07 최신 버전) ──
    {"query": "제1조 보험금의 지급사유", "service_code": "01", "document_id": "0011", "expected_type": "structured_lookup"},
    {"query": "별표1 장해분류표", "service_code": "01", "document_id": "0011", "expected_type": "structured_lookup"},
    {"query": "태아 가입 시 선천이상 수술도 보장이 되나요?", "service_code": "01", "document_id": "0011", "expected_type": "interpretation"},
    {"query": "심신상실 상태에서 자해한 경우 보험금이 나오나요?", "service_code": "01", "document_id": "0011", "expected_type": "interpretation"},
    {"query": "갱신 시 보험료가 인상될 수 있나요?", "service_code": "01", "document_id": "0011", "expected_type": "interpretation"},
    {"query": "지정대리청구인을 변경하려면 어떻게 하나요?", "service_code": "01", "document_id": "0011", "expected_type": "procedure"},
    {"query": "보험계약대출 신청 방법은?", "service_code": "01", "document_id": "0011", "expected_type": "procedure"},
    {"query": "1종과 2종의 차이가 뭔가요?", "service_code": "01", "document_id": "0011", "expected_type": "comparison"},
    {"query": "상해입원일당과 질병입원일당의 차이는?", "service_code": "01", "document_id": "0011", "expected_type": "comparison"},
    {"query": "납입면제 조건이 뭔가요?", "service_code": "01", "document_id": "0011", "expected_type": "simple_fact"},
    {"query": "간병인사용 입원일당 지급 한도는?", "service_code": "01", "document_id": "0011", "expected_type": "simple_fact"},

    # ── 간편건강보험 → 0012 (KB 골든라이프케어 간편건강보험 26.01) ──
    {"query": "보험금을 지급하지 않는 사유", "service_code": "01", "document_id": "0012", "expected_type": "structured_lookup"},
    {"query": "해약환급금 지급 기준", "service_code": "01", "document_id": "0012", "expected_type": "simple_fact"},
    {"query": "위법계약 해지 절차가 어떻게 되나요?", "service_code": "01", "document_id": "0012", "expected_type": "procedure"},
]


# ==============================================================================
# Synthetic feedback (실험적 — 파이프라인 검증 목적만)
# ==============================================================================
# 실 사용자 UI 없는 단계에서 /feedback 수집 + trace_summary --feedback 집계 파이프라인이
# end-to-end로 동작하는지 확인하기 위한 임시 receiver. 매핑 자체(RAGAS 점수 → 사용자
# signal)의 타당성은 검증 안 됐으며, 품질 개선 의사결정 근거로 쓰면 안 됨.
# 실 UI 통합 시 이 함수와 --submit-feedback 플래그 모두 즉시 제거.

def map_score_to_signal(faithfulness: float) -> str:
    """Faithfulness 점수 → signal 매핑 (실험적 — 검증 안 됨, 파이프라인 검증용 proxy)."""
    if faithfulness >= 0.7:
        return "up"
    if faithfulness >= 0.4:
        return "reformulated"
    return "down"


def submit_synthetic_feedback(results: list[dict], faithfulness_by_idx: dict[int, float]) -> None:
    """Per-query Faithfulness → signal 매핑 후 /feedback 엔드포인트로 제출.

    실 운영 데이터와 혼재되지 않도록 free_text에 'synthetic from RAGAS' 명시.
    """
    if not faithfulness_by_idx:
        print("\nSynthetic feedback: faithfulness per-query 점수 없음 (매핑 skip)")
        return

    submitted, failed, skipped = 0, 0, 0
    for i, r in enumerate(results):
        trace_id = r.get("trace_id")
        score = faithfulness_by_idx.get(i)
        if not trace_id or score is None:
            skipped += 1
            continue
        signal = map_score_to_signal(score)
        try:
            resp = requests.post(
                f"{API_URL}/feedback",
                json={
                    "trace_id": trace_id,
                    "signal": signal,
                    "free_text": f"synthetic from RAGAS faithfulness={score:.3f}",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                submitted += 1
            else:
                failed += 1
                print(f"  feedback 실패: {resp.status_code} trace_id={trace_id[:8]}")
        except Exception as e:
            failed += 1
            print(f"  feedback 에러 trace_id={trace_id[:8]}: {e}")

    print(
        f"\n=== Synthetic feedback 제출 완료 ===\n"
        f"  submitted: {submitted} | failed: {failed} | skipped: {skipped}"
    )


def collect_answers():
    """RAG 파이프라인에서 답변 수집."""
    results = []
    for q in EVAL_QUESTIONS:
        doc_id = q.get("document_id", "전체")
        print(f"[{doc_id}] {q['query']}")
        try:
            resp = requests.post(f"{API_URL}/answer", json=q, timeout=120)
            if resp.status_code != 200:
                print(f"  → 에러: {resp.status_code}")
                continue
            data = resp.json()
            route = data.get("route", {})
            predicted_type = route.get("query_type")
            results.append({
                "question": q["query"],
                "document_id": doc_id,
                "answer": data.get("answer", ""),
                "contexts": [s.get("content", "") for s in data.get("sources", [])],
                "route": route,
                "expected_type": q.get("expected_type"),
                "predicted_type": predicted_type,
                "type_match": (predicted_type == q.get("expected_type")) if q.get("expected_type") else None,
                "elapsed_ms": data.get("elapsed_ms", 0),
                "crag_retries": data.get("crag_retries", 0),
                "trace_id": data.get("trace_id"),  # synthetic feedback 매핑용
            })
            match_mark = "✓" if predicted_type == q.get("expected_type") else "✗"
            print(f"  → {predicted_type or '?'} {match_mark} (expected={q.get('expected_type', 'n/a')}) | {data.get('elapsed_ms', 0)}ms")
        except Exception as e:
            print(f"  → 실패: {e}")
    return results


def run_ragas_eval(results):
    """RAGAS 평가 실행."""
    try:
        from ragas import evaluate
        from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
        from ragas.metrics import Faithfulness, ResponseRelevancy, ContextUtilization
        from ragas.llms import LangchainLLMWrapper
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from langchain_openai import ChatOpenAI
        from langchain_community.embeddings import HuggingFaceEmbeddings
    except ImportError:
        print("\nRAGAS 미설치. pip install ragas langchain-openai langchain-community")
        print_basic_stats(results)
        return {}

    # Judge LLM 설정 — 서빙(vLLM/Qwen3)과 계열 분리해서 self-preference bias 회피.
    # Zheng et al. NeurIPS 2023 (MT-Bench)가 같은 계열 LLM이 자기 답변을 체계적으로
    # 과대평가하는 bias를 실증. LLM-as-judge 평가의 1순위 원칙은 "Serving ≠ Judge".
    #
    # 기본: GPT-4o-mini (Triad 짧은 prompt 기준 쿼리당 ~$0.001, 24건 평가셋 ≈ $0.05).
    # OPENAI_API_KEY 미설정 시 vLLM(서빙 LLM)으로 fallback — 이 경우 점수 신뢰도 ↓
    # ("biased" 라벨로 결과 JSON에 명시 + 경고 출력).
    judge_model = os.environ.get("RAGAS_JUDGE_MODEL", "gpt-4o-mini")
    openai_api_key = os.environ.get("OPENAI_API_KEY")

    if openai_api_key:
        judge_llm = LangchainLLMWrapper(ChatOpenAI(
            model=judge_model,
            api_key=openai_api_key,
            temperature=0.0,
            max_tokens=2048,
            timeout=120,
        ))
        judge_label = f"openai/{judge_model}"
    else:
        # 실제 서빙 중인 로컬 모델명. vLLM API상 이름은 "/model"이라 라벨엔 사람이 읽는
        # 이름을 쓴다. 모델 교체 시 LOCAL_LLM_NAME 환경변수로 오버라이드 (기본은 현 로컬 모델).
        local_llm_name = os.environ.get("LOCAL_LLM_NAME", "Qwen3-4B-AWQ")
        print(f"\n⚠ OPENAI_API_KEY 미설정 — 로컬 vLLM judge({local_llm_name})로 fallback.")
        print("  serving과 동일 모델이면 self-preference bias 위험. 점수는 참고용으로만 사용.")
        print("  운영 평가는 OPENAI_API_KEY 설정 후 재실행 권장 (Triad 24건 ≈ $0.05).\n")
        judge_llm = LangchainLLMWrapper(ChatOpenAI(
            model="/model",
            base_url="http://localhost:8000/v1",
            api_key="no-key",
            temperature=0.0,
            max_tokens=2048,
            timeout=120,
        ))
        judge_label = f"vllm/{local_llm_name} (self-judge, biased)"

    # Embeddings는 RAGAS 내부 의미 비교용 — judge LLM과 무관해서 BGE-M3 그대로 (로컬, 무료).
    judge_embeddings = LangchainEmbeddingsWrapper(HuggingFaceEmbeddings(
        model_name="model/BGE-M3",
        model_kwargs={"device": "cpu"},
    ))

    samples = [
        SingleTurnSample(
            user_input=r["question"],
            response=_clean_answer(r["answer"]),
            retrieved_contexts=r["contexts"],
        )
        for r in results if r["answer"]
    ]

    dataset = EvaluationDataset(samples=samples)

    print(f"\n=== RAGAS 평가 중 ({len(samples)}개 질문, judge={judge_label})... ===\n")
    result = evaluate(
        dataset=dataset,
        metrics=[Faithfulness(), ResponseRelevancy(), ContextUtilization()],
        llm=judge_llm,
        embeddings=judge_embeddings,
    )

    # 점수 추출
    scores = {}
    try:
        scores = result._repr_dict if hasattr(result, '_repr_dict') else {}
    except Exception:
        pass
    if not scores:
        try:
            df = result.to_pandas()
            scores = {col: df[col].mean() for col in df.columns if df[col].dtype == float}
        except Exception:
            pass

    # 결과 출력
    print(f"{'='*50}")
    print(f"  RAGAS 평가 결과")
    print(f"{'='*50}")
    for k, v in scores.items():
        if isinstance(v, float) and v == v:
            bar = "█" * int(v * 20)
            print(f"  {k:25s}: {v:.4f} {bar}")
    print(f"{'='*50}")

    # per-query Faithfulness 추출 (synthetic feedback 매핑용).
    # RAGAS 버전별 컬럼 이름 차이 대응 — faithfulness / Faithfulness / faithfulness_score.
    faithfulness_by_idx: dict[int, float] = {}
    try:
        df = result.to_pandas()
        col = next(
            (c for c in ("faithfulness", "Faithfulness", "faithfulness_score") if c in df.columns),
            None,
        )
        if col is not None:
            for i, v in enumerate(df[col]):
                try:
                    fv = float(v)
                    if fv == fv:  # NaN skip
                        faithfulness_by_idx[i] = fv
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass

    # 기본 통계도 함께 출력
    print_basic_stats(results)

    # 저장 — judge_llm 라벨 박아 사후 audit 가능 (serving과 분리됐는지 검증).
    output = {
        "timestamp": datetime.now().isoformat(),
        "judge_llm": judge_label,                       # ex: "openai/gpt-4o-mini"
        "serving_llm": "vllm/qwen3-14b-awq",            # 서빙 파이프라인 LLM
        "self_preference_bias_risk": "biased" in judge_label,  # 같은 계열이면 True
        "metrics": {k: v for k, v in scores.items() if isinstance(v, float)},
        "question_count": len(results),
        "details": results,
    }
    with open("data/eval/ragas_eval_result.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n결과 저장: data/eval/ragas_eval_result.json")

    return faithfulness_by_idx


def print_basic_stats(results):
    """기본 통계."""
    if not results:
        return

    print(f"\n--- 기본 통계 ---")
    print(f"  총 질문: {len(results)}")

    # 응답 시간
    times = [r["elapsed_ms"] for r in results]
    print(f"  평균 응답: {sum(times) / len(times):.0f}ms (min={min(times)}, max={max(times)})")

    # 쿼리 유형 분포
    routes = {}
    for r in results:
        qt = r["route"].get("query_type", "unknown")
        routes[qt] = routes.get(qt, 0) + 1
    print(f"  쿼리 유형: {routes}")

    # Routing accuracy — classifier가 expected_type과 일치하는 비율.
    # 분류기 회귀 감지용. 평가셋 24개 규모라 통계 의미 약하지만 패턴 변화는 감지 가능.
    labeled = [r for r in results if r.get("expected_type")]
    if labeled:
        correct = sum(1 for r in labeled if r.get("type_match"))
        accuracy = correct / len(labeled)
        print(f"  Routing accuracy: {correct}/{len(labeled)} ({accuracy:.1%})")
        # 틀린 케이스 샘플 — 패턴 디버깅용
        wrong = [r for r in labeled if r.get("type_match") is False]
        for r in wrong[:5]:
            print(f"    ✗ \"{r['question'][:40]}\": {r['expected_type']} → {r['predicted_type']}")

    # 문서별 분포
    docs = {}
    for r in results:
        d = r.get("document_id", "?")
        docs[d] = docs.get(d, 0) + 1
    print(f"  문서별: {docs}")

    # CRAG 재검색
    crag = sum(1 for r in results if r.get("crag_retries", 0) > 0)
    print(f"  CRAG 재검색: {crag}/{len(results)}건")

    # "확인되지 않음" 비율
    no_answer = sum(1 for r in results if "확인되지 않음" in r.get("answer", ""))
    print(f"  답변 불가: {no_answer}/{len(results)}건")


def main():
    parser = argparse.ArgumentParser(description="RAGAS 평가")
    parser.add_argument("--basic", action="store_true", help="RAGAS 없이 기본 통계만")
    parser.add_argument(
        "--submit-feedback", action="store_true",
        help="[실험적] RAGAS Faithfulness를 가짜 사용자 signal로 매핑해 /feedback 제출 "
             "(up ≥ 0.7 / reformulated ≥ 0.4 / down < 0.4). 매핑 자체 검증 안 됨 — "
             "수집·집계 파이프라인 동작 확인 목적만. 실 UI 통합 시 제거.",
    )
    args = parser.parse_args()

    # 평가 질문셋: 기본은 위 EVAL_QUESTIONS(원본 24문항, 실 코퍼스용). 로컬에 다른 문서를
    # 적재해 검증할 땐 EVAL_QUESTIONS_FILE=<경로>로 교체 (JSON 배열, 같은 스키마).
    global EVAL_QUESTIONS
    _qfile = os.environ.get("EVAL_QUESTIONS_FILE")
    if _qfile:
        with open(_qfile, encoding="utf-8") as f:
            EVAL_QUESTIONS = json.load(f)
        print(f"질문셋 교체: {_qfile} ({len(EVAL_QUESTIONS)}문항)\n")

    os.makedirs("data/eval", exist_ok=True)  # 결과 저장 경로 보장 (import 부작용 회피, main에서만 호출)

    print("=== docs-rag RAGAS 평가 ===\n")
    results = collect_answers()

    if not results:
        print("답변을 수집하지 못했습니다.")
        return

    if args.basic:
        print_basic_stats(results)
        with open("data/eval/ragas_eval_result.json", "w", encoding="utf-8") as f:
            json.dump({"details": results}, f, ensure_ascii=False, indent=2)
        print(f"\n답변 데이터 저장: data/eval/ragas_eval_result.json")
        if args.submit_feedback:
            print("\n[--basic 모드는 faithfulness 점수가 없어 feedback 제출 skip]")
    else:
        faithfulness_by_idx = run_ragas_eval(results)
        if args.submit_feedback:
            submit_synthetic_feedback(results, faithfulness_by_idx)


if __name__ == "__main__":
    main()
