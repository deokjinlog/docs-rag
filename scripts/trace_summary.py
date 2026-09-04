"""서빙 경로 trace 집계 리포트.

쿼리별 JSONL을 12 섹션으로 집계:
  Request Volume / Route / Decomposition / Rerank / CRAG /
  Verification(+groundedness 0~1 percentile) / Provenance / Latency / Errors /
  Critic / Input Guard (PII + Injection) / Output Guard (leak + profanity)

--feedback 플래그로 13번째 섹션(Feedback DB JOIN) 추가 — DB feedback과 trace JSONL을
trace_id로 조인해 signal 분포·매칭률·signal별 rerank·risk 상관 출력.
DB(sqlalchemy) 의존성은 --feedback 호출 시만 import (trace 단독 실행에 영향 없음).

사용법:
    uv run python scripts/trace_summary.py                        # 당일
    uv run python scripts/trace_summary.py --date 20260421
    uv run python scripts/trace_summary.py --from 20260415 --to 20260421
    uv run python scripts/trace_summary.py --endpoint answer --service 01
    uv run python scripts/trace_summary.py --feedback --days 7    # feedback 섹션 추가

출력: stdout + data/eval/trace_summary_<YYYYMMDD>.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# --- 상수 ---
TRACE_ROOT = Path("data/eval/trace")
CRAG_SCORE_THRESHOLD = 0.3          # src/v1/config/settings.py의 CRAG_SCORE_THRESHOLD와 일치 유지
RERANK_BIN_SIZE = 0.1               # histogram bin 크기
PERCENTILES = (10, 25, 50, 75, 90, 99)
LATENCY_SPANS = (
    "query_embed", "qdrant_search", "rerank", "sibling_expand",
    "context_truncate", "llm_generate", "verify", "total",
)

Records = list[dict[str, Any]]


# --- CLI & 로딩 ---------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trace JSONL 집계 + 옵션 Feedback DB 집계")
    p.add_argument("--date", help="YYYYMMDD (단일 날짜)")
    p.add_argument("--from", dest="from_date", help="YYYYMMDD 시작일")
    p.add_argument("--to", dest="to_date", help="YYYYMMDD 종료일 (포함)")
    p.add_argument("--endpoint", choices=["answer", "retrieve"], help="엔드포인트 필터")
    p.add_argument("--service", help="service_code 필터")
    p.add_argument("--feedback", action="store_true",
                   help="Feedback DB 집계 추가 (signal·매칭률·signal별 rerank·risk)")
    p.add_argument("--days", type=int, default=7,
                   help="--feedback 기간 (기본 7일)")
    return p.parse_args()


def resolve_date_dirs(args: argparse.Namespace) -> list[Path]:
    """CLI 인자 → 스캔할 날짜 디렉토리 목록."""
    if args.date:
        return [TRACE_ROOT / args.date]
    if args.from_date and args.to_date:
        start = datetime.strptime(args.from_date, "%Y%m%d")
        end = datetime.strptime(args.to_date, "%Y%m%d")
        days, cur = [], start
        while cur <= end:
            days.append(TRACE_ROOT / cur.strftime("%Y%m%d"))
            cur += timedelta(days=1)
        return days
    return [TRACE_ROOT / datetime.now(timezone.utc).strftime("%Y%m%d")]


def load_traces(dirs: list[Path], args: argparse.Namespace) -> Iterator[dict[str, Any]]:
    """JSONL 스트리밍 로드 + 필터 적용. 파손 라인은 skip + warn."""
    for d in dirs:
        fp = d / "traces.jsonl"
        if not fp.exists():
            continue
        with fp.open(encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(f"[warn] {fp}:{i} parse error: {exc}")
                    continue
                if args.endpoint and rec.get("endpoint") != args.endpoint:
                    continue
                if args.service and rec.get("request", {}).get("service_code") != args.service:
                    continue
                yield rec


# --- 통계 유틸 -----------------------------------------------------------------

def percentiles(
    values: list[float],
    ps: tuple[int, ...] = PERCENTILES,
) -> dict[str, float | None]:
    """n<100은 sort+index, 이상은 statistics.quantiles."""
    if not values:
        return {f"p{p}": None for p in ps}
    if len(values) < 100:
        sv = sorted(values)
        return {f"p{p}": round(sv[min(len(sv) - 1, int(len(sv) * p / 100))], 4) for p in ps}
    q = statistics.quantiles(sorted(values), n=100, method="inclusive")
    return {f"p{p}": round(q[p - 1], 4) for p in ps}


def histogram(
    values: list[float],
    bin_size: float = RERANK_BIN_SIZE,
    lo: float = 0.0,
    hi: float = 1.0,
) -> dict[str, int]:
    """[lo, hi]를 bin_size 간격으로 나눠 counts 반환."""
    n_bins = int(round((hi - lo) / bin_size))
    keys = [f"{lo + i * bin_size:.1f}-{lo + (i + 1) * bin_size:.1f}" for i in range(n_bins)]
    bins: dict[str, int] = {k: 0 for k in keys}
    for v in values:
        idx = min(max(int((v - lo) / bin_size), 0), n_bins - 1)
        bins[keys[idx]] += 1
    return bins


# --- 섹션별 집계 ---------------------------------------------------------------

def _aggregate_volume(records: Records) -> dict[str, Any]:
    endpoints = Counter(r.get("endpoint", "?") for r in records)
    errors = [r for r in records if r.get("error")]
    return {
        "total": len(records),
        "endpoints": dict(endpoints),
        "errors_count": len(errors),
    }


def _aggregate_route(records: Records) -> dict[str, Any]:
    routed = [r for r in records if r.get("route")]
    return {
        "strategies": dict(Counter((r["route"] or {}).get("strategy", "?") for r in routed)),
        "query_types": dict(Counter((r["route"] or {}).get("query_type", "?") for r in routed)),
    }


def _aggregate_answerability(records: Records) -> dict[str, Any]:
    """답을 냈나 못 냈나 — 거절률 + SQL 결정론 적중률.

    두 가지를 구분해서 센다(같은 '답 못 냄'이 아니다):
      · refusal        "관련 내용을 찾지 못했습니다" — 검색이 근거를 못 찾음(품질 신호)
      · sql_hit_rate   /answer 중 결정론 SQL로 답한 비율 — 낮아지면 라우팅/데이터 회귀 신호
    SQL 미적중은 그 자체로는 정상이다(precision-first로 RAG에 넘김). 그래서 '거절'로 세지
    않고 적중률로만 본다. 추세가 지표 — 절대값보다 어제 대비 변화가 의미 있다.
    """
    ans = [r for r in records if r.get("endpoint") == "answer"]
    with_ans = [r for r in ans if r.get("answer") is not None]
    refusals = [r for r in with_ans if (r.get("answer") or {}).get("is_refusal")]
    sql = [r for r in ans if (r.get("route") or {}).get("strategy") == "sql"]
    return {
        "answer_total": len(ans),
        "answer_traced": len(with_ans),          # answer 필드가 있는 것(집계 가능 모수)
        "refusal_count": len(refusals),
        "refusal_pct": round(len(refusals) / len(with_ans) * 100, 1) if with_ans else None,
        "sql_hit_count": len(sql),
        "sql_hit_pct": round(len(sql) / len(ans) * 100, 1) if ans else None,
    }


def _aggregate_decomposition(records: Records) -> dict[str, Any]:
    comp = [r for r in records if (r.get("route") or {}).get("query_type") == "comparison"]
    methods = Counter((r.get("decomposition") or {}).get("method", "none") for r in comp)
    return {
        "comparison_total": len(comp),
        "decomposition_methods": dict(methods),
    }


def _aggregate_rerank(records: Records) -> dict[str, Any]:
    """top-1 점수 분포 + **top1↔top2 격차**.

    Why 격차: 답하기 전에 "모른다"를 알 수 있는 거의 유일한 신호다.
      · top-1 점수가 낮다      → 도메인 밖 질문 (검색이 아무것도 못 찾음)
      · 격차가 작다(동점권)     → 모호한 질문 (여러 청크가 똑같이 그럴듯함)
    둘 다 **저신뢰 검색인데 답변까지 간** 조용한 실패(silent failure)의 전조다.
    임계는 아직 안 정한다 — 분포를 먼저 보고 데이터가 임계를 말하게 한다(measure-first).
    """
    top1: list[float] = []
    gaps: list[float] = []
    lowconf = 0
    for r in records:
        scores = (r.get("retrieval") or {}).get("rerank_scores") or []
        if not scores:
            continue
        top1.append(float(scores[0]))
        if len(scores) >= 2:
            gaps.append(round(float(scores[0]) - float(scores[1]), 4))
        # CRAG 재검색 임계(0.3)를 저신뢰 기준으로 재사용 — 그 아래인데 답변까지 간 건수
        if float(scores[0]) < 0.3 and r.get("endpoint") == "answer":
            lowconf += 1
    return {
        "rerank_top1": {
            "percentiles": percentiles(top1),
            "histogram": histogram(top1),
            "mean": round(statistics.mean(top1), 4) if top1 else None,
            "count": len(top1),
        },
        "rerank_gap": {
            "percentiles": percentiles(gaps),
            "mean": round(statistics.mean(gaps), 4) if gaps else None,
            "count": len(gaps),
        },
        "low_confidence_answers": lowconf,
    }


def _aggregate_crag(records: Records) -> dict[str, Any]:
    answers = [r for r in records if r.get("endpoint") == "answer"]
    triggered = [r for r in answers if (r.get("crag") or {}).get("retries", 0) > 0]

    deltas: list[float] = []
    improved = 0
    for r in triggered:
        crag = r.get("crag") or {}
        before, after = crag.get("score_before"), crag.get("score_after")
        if before is not None and after is not None:
            deltas.append(after - before)
            if after > before:
                improved += 1

    below_threshold = [
        r for r in answers
        if ((r.get("crag") or {}).get("score_after") or 0) < CRAG_SCORE_THRESHOLD
    ]

    return {
        "crag": {
            "answer_total": len(answers),
            "trigger_count": len(triggered),
            "improved_count": improved,
            "avg_delta": round(statistics.mean(deltas), 4) if deltas else None,
            "final_below_threshold_count": len(below_threshold),
        }
    }


def _aggregate_verification(records: Records) -> dict[str, Any]:
    verified = [r for r in records if r.get("verification")]
    levels = Counter(r["verification"].get("risk_level", "?") for r in verified)

    # Groundedness 0~1 분포 — Azure AI Foundry / RAGAS faithfulness 형태.
    # risk_level 4단계 라벨이 못 보여주는 추세·A/B 비교·SLA 임계 박는 데 사용.
    grounds = [
        r["verification"]["groundedness"]
        for r in verified
        if "groundedness" in r["verification"]
    ]
    if grounds:
        grounds_sorted = sorted(grounds)
        n = len(grounds_sorted)
        groundedness_stats = {
            "count": n,
            "mean": round(statistics.mean(grounds), 3),
            "p50": round(grounds_sorted[n // 2], 3),
            "p95": round(grounds_sorted[min(n - 1, int(n * 0.95))], 3),
            "min": round(grounds_sorted[0], 3),
        }
    else:
        groundedness_stats = None

    return {"risk_levels": dict(levels), "groundedness": groundedness_stats}


def _aggregate_provenance(records: Records) -> dict[str, Any]:
    """avg_claims_per_answer는 전체 claim 평균 (답변 길이 신호용),
    coverage_pct는 verifiable claim 분모 — groundedness와 동일 정의 (RAGAS faithfulness 패턴).
    """
    triples = [
        (v["claims_count"], v.get("verifiable_claims_count", 0), v["supported_claims_count"])
        for r in records
        if (v := r.get("verification")) and v.get("claims_count")
    ]
    if not triples:
        return {"provenance": {"avg_claims_per_answer": None, "coverage_pct": None}}
    total_verifiable = sum(v for _, v, _ in triples)
    total_supported = sum(s for _, _, s in triples)
    return {
        "provenance": {
            "avg_claims_per_answer": round(statistics.mean(c for c, _, _ in triples), 2),
            "coverage_pct": (
                round(total_supported / total_verifiable * 100, 1) if total_verifiable else None
            ),
        }
    }


def _aggregate_latency(records: Records) -> dict[str, Any]:
    out: dict[str, dict[str, float | int]] = {}
    for span in LATENCY_SPANS:
        vals = [float(v) for r in records if (v := r.get("timing_ms", {}).get(span)) is not None]
        if vals:
            out[span] = {
                "p50": percentiles(vals, (50,))["p50"],
                "p95": percentiles(vals, (95,))["p95"],
                "count": len(vals),
            }
    return {"latency_ms": out}


def _aggregate_errors(records: Records) -> dict[str, Any]:
    errors = [r for r in records if r.get("error")]
    return {"error_types": dict(Counter((r["error"] or {}).get("type", "?") for r in errors))}


def _aggregate_critic(records: Records) -> dict[str, Any]:
    """Critic dispatch 집계 — invocation rate, failure_type 분포, regenerate 효과.

    Critic 운영 효과 측정 근거 데이터.
    """
    traces = list(records)
    verified = [t for t in traces if t.get("verification")]
    critic_traces = [t for t in traces if (t.get("critic") or {}).get("invoked")]

    by_failure = Counter(
        (t.get("critic") or {}).get("failure_type")
        for t in critic_traces
        if (t.get("critic") or {}).get("failure_type")
    )
    by_action = Counter(
        (t.get("critic") or {}).get("action_taken")
        for t in critic_traces
        if (t.get("critic") or {}).get("action_taken")
    )

    regenerated = [
        t for t in critic_traces
        if (t.get("critic") or {}).get("action_taken") == "regenerate"
    ]
    improved = sum(1 for t in regenerated if (t.get("critic") or {}).get("regenerate_improved"))
    regen_improved_rate = improved / len(regenerated) if regenerated else 0.0

    transitions = Counter(
        ((t.get("critic") or {}).get("before_risk"), (t.get("critic") or {}).get("after_risk"))
        for t in regenerated
    )

    # critic이 분석 후 "회복 불가/무의미"로 pass 판정한 케이스의 before_risk 분포.
    # silent warn(critic 미호출)과 구분되는 핵심 운영 신호 — 이 비율이 높으면
    # retrieval 품질이 흔들려 critic이 개입할 정도로 mismatch가 발생하는데
    # 거리가 멀어 regenerate로 못 고치는 패턴 (재현된 hint도 무용).
    pass_action_origins = Counter(
        (t.get("critic") or {}).get("before_risk")
        for t in critic_traces
        if (t.get("critic") or {}).get("action_taken") == "pass"
        and (t.get("critic") or {}).get("before_risk")
    )

    return {"critic": {
        "total_verified": len(verified),
        "invoked": len(critic_traces),
        "invocation_rate": (len(critic_traces) / len(verified)) if verified else 0.0,
        "by_failure_type": dict(by_failure),
        "by_action": dict(by_action),
        "regenerated_count": len(regenerated),
        "regenerate_improved": improved,
        "regenerate_improved_rate": round(regen_improved_rate, 3),
        "risk_before_after": [
            {"from": k[0], "to": k[1], "count": v} for k, v in transitions.most_common()
        ],
        "pass_action_origins": dict(pass_action_origins),
    }}


def _aggregate_input_guard(records: Records) -> dict[str, Any]:
    """Input Guard 발동 통계 — PII 마스킹 + Prompt Injection.

    PII: 종류별 카운트로 도메인 위험도 / DLP 도구 도입 우선순위 판단.
    Injection: zero-width / role token / "이전 지시 무시" 등의 시도가 실제 트래픽에
    얼마나 들어오나 가시화 — Lakera/Rebuff 같은 LLM judge 도입 정당화 근거.
    """
    has_guard = [r for r in records if r.get("input_guard")]
    pii_flagged = [r for r in has_guard if (r.get("input_guard") or {}).get("pii_count", 0) > 0]
    by_pii: Counter[str] = Counter()
    for r in pii_flagged:
        for k in (r.get("input_guard") or {}).get("pii_found", []):
            by_pii[k] += 1

    injection_flagged = [
        r for r in has_guard
        if (r.get("input_guard") or {}).get("injection_threats")
    ]
    by_injection: Counter[str] = Counter()
    for r in injection_flagged:
        for t in (r.get("input_guard") or {}).get("injection_threats", []):
            by_injection[t] += 1

    return {"input_guard": {
        "total_with_guard": len(has_guard),
        "pii_flagged_count": len(pii_flagged),
        "pii_flagged_rate": (len(pii_flagged) / len(has_guard)) if has_guard else 0.0,
        "by_pii_kind": dict(by_pii.most_common()),
        "injection_flagged_count": len(injection_flagged),
        "injection_flagged_rate": (len(injection_flagged) / len(has_guard)) if has_guard else 0.0,
        "by_injection_threat": dict(by_injection.most_common()),
    }}


def _aggregate_output_guard(records: Records) -> dict[str, Any]:
    """Output Guard 발동 통계 — role token leak / 욕설.

    leak rate ↑ = LLM이 system prompt를 답변에 흘리는 회귀 (프롬프트 / 모델 점검 필요).
    profanity rate ↑ = 사용자 쿼리 trigger 또는 모델 alignment 회귀.
    """
    has_output = [r for r in records if r.get("output_guard")]
    flagged = [r for r in has_output if (r.get("output_guard") or {}).get("threats")]
    by_threat: Counter[str] = Counter()
    for r in flagged:
        for t in (r.get("output_guard") or {}).get("threats", []):
            kind = "leak" if t.startswith("output_leak:") else (
                "profanity" if t.startswith("profanity:") else "other"
            )
            by_threat[kind] += 1
    return {"output_guard": {
        "total_with_guard": len(has_output),
        "flagged_count": len(flagged),
        "flagged_rate": (len(flagged) / len(has_output)) if has_output else 0.0,
        "by_threat_kind": dict(by_threat.most_common()),
    }}


AGGREGATORS = (
    _aggregate_volume,
    _aggregate_route,
    _aggregate_answerability,
    _aggregate_decomposition,
    _aggregate_rerank,
    _aggregate_crag,
    _aggregate_verification,
    _aggregate_provenance,
    _aggregate_latency,
    _aggregate_errors,
    _aggregate_critic,
    _aggregate_input_guard,
    _aggregate_output_guard,
)


def aggregate(records: Records) -> dict[str, Any]:
    """12 섹션을 단일 summary dict로 조립 (volume·route·decomposition·rerank·crag·verification·provenance·latency·errors·critic·input_guard·output_guard)."""
    summary: dict[str, Any] = {}
    for fn in AGGREGATORS:
        summary.update(fn(records))
    return summary


# --- 렌더링 -------------------------------------------------------------------

def _pct(d: dict[str, int], k: str, total: int) -> str:
    return f"{k} {d[k] / total * 100:.1f}%" if total else k


def _bar(count: int, maximum: int, width: int = 20) -> str:
    return "█" * min(int(count / max(maximum, 1) * width), width)


def _render_header(summary: dict[str, Any], period: str) -> None:
    n = summary["total"]
    print("=== docs-rag Trace Summary ===")
    print(f"Period: {period}, Total requests: {n}")
    if n == 0:
        print("  (no traces)")
        return
    eps = summary["endpoints"]
    print(
        f"  /retrieve {eps.get('retrieve', 0)} · /answer {eps.get('answer', 0)} "
        f"· errors {summary['errors_count']}\n"
    )


def _render_route(summary: dict[str, Any]) -> None:
    print("─── Route Distribution ────────────────────────")
    n = summary["total"]
    strat, qt = summary["strategies"], summary["query_types"]
    print("  strategy:   " + " | ".join(_pct(strat, k, n) for k in strat))
    print("  query_type: " + " | ".join(_pct(qt, k, n) for k in qt))
    print()


def _render_answerability(summary: dict[str, Any]) -> None:
    print("─── Answerability (거절률 · SQL 적중률) ──────────────────────")
    tot, traced = summary["answer_total"], summary["answer_traced"]
    if tot == 0:
        print("  /answer trace 없음")
        print()
        return
    miss = tot - traced
    rp, sp = summary["refusal_pct"], summary["sql_hit_pct"]
    print(f"  /answer {tot}건 (answer 필드 있음 {traced}"
          + (f", 누락 {miss} — 구버전 trace" if miss else "") + ")")
    print(f"  거절(근거 못 찾음): {summary['refusal_count']}건"
          + (f" = {rp}%" if rp is not None else ""))
    print(f"  SQL 결정론 적중:    {summary['sql_hit_count']}건"
          + (f" = {sp}%" if sp is not None else "")
          + "   ← 낮아지면 라우팅·데이터 회귀 신호")
    print()


def _render_decomposition(summary: dict[str, Any]) -> None:
    cn = summary["comparison_total"]
    print(f"─── Decomposition (COMPARISON만, n={cn}) ──────")
    if cn == 0:
        print("  (no COMPARISON queries)\n")
        return
    dm = summary["decomposition_methods"]
    parts = [f"{k} {dm.get(k, 0) / cn * 100:.1f}%" for k in ("rule", "llm", "llm_failed", "none")]
    print("  " + " | ".join(parts))
    print()


def _render_rerank(summary: dict[str, Any]) -> None:
    rt = summary["rerank_top1"]
    print(f"─── Rerank Score Distribution (top-1, n={rt['count']}) ────")
    if rt["count"] == 0:
        print()
        return
    p = rt["percentiles"]
    print(
        f"  p10={p['p10']}  p25={p['p25']}  p50={p['p50']}  "
        f"p75={p['p75']}  p90={p['p90']}  p99={p['p99']}  mean={rt['mean']}"
    )
    g = summary.get("rerank_gap") or {}
    if g.get("count"):
        gp = g["percentiles"]
        print(f"  top1↔top2 격차: p10={gp['p10']} p50={gp['p50']} p90={gp['p90']} "
              f"mean={g['mean']}   ← 작으면 모호(동점권)")
    lc = summary.get("low_confidence_answers", 0)
    print(f"  저신뢰(top1<0.3) 답변: {lc}건" + ("   ← 조용한 실패 후보" if lc else ""))
    hist = rt["histogram"]
    max_count = max(hist.values()) or 1
    for bin_range, count in hist.items():
        print(f"  {bin_range}: {count:>4} {_bar(count, max_count)}")
    print()


def _render_crag(summary: dict[str, Any]) -> None:
    cr = summary["crag"]
    at = cr["answer_total"]
    print("─── CRAG Effectiveness ───────────────────────")
    if at == 0:
        print("  (no /answer traces)\n")
        return
    trig = cr["trigger_count"]
    print(f"  재시도 트리거:      {trig} / {at} ({trig / at * 100:.1f}%)")
    if trig > 0:
        imp = cr["improved_count"]
        print(f"  재시도 후 score 개선: {imp} / {trig} ({imp / trig * 100:.1f}%)")
        print(f"  평균 Δscore:        {cr['avg_delta']}")
    below = cr["final_below_threshold_count"]
    print(f"  최종 threshold 미달:  {below} / {at} ({below / at * 100:.1f}%)")
    print()


def _render_verification(summary: dict[str, Any]) -> None:
    print("─── Verification risk_level ──────────────────")
    rl = summary["risk_levels"]
    total = sum(rl.values())
    if total == 0:
        print("  (no verification records)\n")
        return
    for k in ("pass", "warn", "soft_fail", "hard_fail"):
        c = rl.get(k, 0)
        print(f"  {k:10s} {c:>4} ({c / total * 100:.1f}%)")
    g = summary.get("groundedness")
    if g:
        print(
            f"  Groundedness  mean={g['mean']:.3f}  p50={g['p50']:.3f}  "
            f"p95={g['p95']:.3f}  min={g['min']:.3f}  (n={g['count']})"
        )
    print()


def _render_provenance(summary: dict[str, Any]) -> None:
    print("─── Claim-근거 매핑 Coverage ──────────────────")
    pv = summary["provenance"]
    if pv["avg_claims_per_answer"] is None:
        print("  (no claims data)\n")
        return
    print(f"  평균 claims/answer: {pv['avg_claims_per_answer']}  (전체 분해 claim — 답변 길이 신호)")
    cov = pv["coverage_pct"]
    cov_str = f"{cov}%" if cov is not None else "n/a (verifiable claim 0)"
    print(f"  supported/verifiable: {cov_str}  (조항/숫자 추출된 claim 분모)")
    print()


def _render_latency(summary: dict[str, Any]) -> None:
    print("─── Latency Breakdown (ms, p50 / p95) ────────")
    lat = summary["latency_ms"]
    for span in LATENCY_SPANS:
        if span in lat:
            s = lat[span]
            print(f"  {span:20s} {s['p50']:>7} / {s['p95']:>7}  (n={s['count']})")
    print()


def _render_errors(summary: dict[str, Any]) -> None:
    et = summary["error_types"]
    if not et:
        return
    print(f"─── Errors (n={summary['errors_count']}) ─────────────────────────────")
    for k, v in et.items():
        print(f"  {k}: {v}")
    print()


def _render_critic(summary: dict[str, Any]) -> None:
    """Critic dispatch 통계. invocation 0이어도 헤더는 출력."""
    c = summary.get("critic")
    if not c:
        return
    print("─── Critic Dispatch ───────────────────────────")
    print(f"  verified traces:           {c['total_verified']}")
    print(f"  critic invoked:            {c['invoked']} ({c['invocation_rate']:.1%})")
    if c["by_failure_type"]:
        print(f"  failure types:             {c['by_failure_type']}")
    if c["by_action"]:
        print(f"  actions:                   {c['by_action']}")
    if c["regenerated_count"]:
        print(
            f"  regenerate improved:       {c['regenerate_improved']}/{c['regenerated_count']} "
            f"({c['regenerate_improved_rate']:.1%})  [target ≥ 40%]"
        )
        for t in c["risk_before_after"][:5]:
            print(f"    {t['from']} → {t['to']}: {t['count']}")
    if c.get("pass_action_origins"):
        # silent warn(critic 미호출)과 구분 — critic이 개입했지만 회복 불가 판정한 케이스.
        # soft_fail 비중 높으면 retrieval 품질 의심 (mismatch는 발생하는데 거리가 멀어 못 고침).
        origins = " | ".join(f"{k} {v}" for k, v in c["pass_action_origins"].items())
        print(f"  critic-passed origins:     {origins}")
    print()


def _render_input_guard(summary: dict[str, Any]) -> None:
    """Input Guard 발동 통계 — PII 마스킹 + Prompt Injection."""
    g = summary.get("input_guard")
    if not g or g["total_with_guard"] == 0:
        return
    print("─── Input Guard ───────────────────────────────")
    print(f"  guard 적용 요청:      {g['total_with_guard']}")
    print(
        f"  PII 감지 요청:        {g['pii_flagged_count']} ({g['pii_flagged_rate']:.1%})"
    )
    if g["by_pii_kind"]:
        kinds = " | ".join(f"{k} {v}" for k, v in g["by_pii_kind"].items())
        print(f"  PII 종류별:           {kinds}")
    print(
        f"  Injection 감지 요청:  {g['injection_flagged_count']} ({g['injection_flagged_rate']:.1%})"
    )
    if g["by_injection_threat"]:
        threats = " | ".join(f"{k} {v}" for k, v in list(g["by_injection_threat"].items())[:5])
        print(f"  Injection 종류별:     {threats}")
    print()


def _render_output_guard(summary: dict[str, Any]) -> None:
    """Output Guard 발동 통계 — leak / 욕설."""
    g = summary.get("output_guard")
    if not g or g["total_with_guard"] == 0:
        return
    print("─── Output Guard ──────────────────────────────")
    print(f"  guard 적용 답변:      {g['total_with_guard']}")
    print(
        f"  threat 감지 답변:     {g['flagged_count']} ({g['flagged_rate']:.1%})"
    )
    if g["by_threat_kind"]:
        kinds = " | ".join(f"{k} {v}" for k, v in g["by_threat_kind"].items())
        print(f"  threat 종류별:        {kinds}")
    print()


RENDERERS = (
    _render_route,
    _render_answerability,
    _render_decomposition,
    _render_rerank,
    _render_crag,
    _render_verification,
    _render_provenance,
    _render_latency,
    _render_errors,
    _render_critic,
    _render_input_guard,
    _render_output_guard,
)


def render(summary: dict[str, Any], period: str) -> None:
    """12 섹션 스탠드아웃 리포트."""
    _render_header(summary, period)
    if summary["total"] == 0:
        return
    for fn in RENDERERS:
        fn(summary)


# --- 저장 ---------------------------------------------------------------------

def _format_period(dirs: list[Path]) -> str:
    if len(dirs) <= 3:
        return ", ".join(d.name for d in dirs)
    return f"{dirs[0].name}..{dirs[-1].name}"


def save_summary(summary: dict[str, Any], args: argparse.Namespace, period: str) -> Path:
    """summary를 data/eval/trace_summary_<YYYYMMDD>.json으로 저장.

    TRACE_ROOT(=data/eval/trace)는 Docker 컨테이너가 생성·소유하므로
    host user가 write 불가 → TRACE_ROOT.parent(=data/eval/)에 저장.
    eval_ragas.py·eval_index_health.py와 동일한 패턴.
    """
    out_dir = TRACE_ROOT.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = out_dir / f"trace_summary_{today}.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": period,
        "filters": {k: v for k, v in vars(args).items() if v is not None},
        **summary,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


# --- Feedback (옵션, --feedback 플래그) --------------------------------------
# DB feedback과 trace JSONL을 trace_id로 조인. sqlalchemy/DB 의존성은 함수 내부
# lazy import — trace 단독 실행에는 영향 없음. host 실행 시 PG_HOST 자동 override.

def _load_feedback(days: int) -> list[dict]:
    """tb_query_feedback 최근 N일 로드. raw SQL — ORM 회피, 집계 전용."""
    if os.environ.get("PG_HOST", "postgres") == "postgres" and not Path("/.dockerenv").exists():
        os.environ["PG_HOST"] = "localhost"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sqlalchemy import text
    from src.v1.config import get_db

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    db_gen = get_db()
    db = next(db_gen)
    try:
        rows = db.execute(
            text("""
                SELECT id, trace_id, signal, free_text, created_at
                FROM tb_query_feedback
                WHERE created_at >= :cutoff
                ORDER BY created_at DESC
            """),
            {"cutoff": cutoff},
        ).mappings().all()
        return [dict(r) for r in rows]
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


def _load_traces_by_id(days: int) -> dict[str, dict]:
    """최근 N일 trace JSONL을 trace_id → record dict로. feedback JOIN 키."""
    by_id: dict[str, dict] = {}
    for i in range(days + 1):
        date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y%m%d")
        fp = TRACE_ROOT / date / "traces.jsonl"
        if not fp.exists():
            continue
        with fp.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = rec.get("trace_id")
                if tid:
                    by_id[tid] = rec
    return by_id


def _aggregate_feedback(feedbacks: list[dict], traces: dict[str, dict]) -> dict[str, Any]:
    """4종 지표: signal 분포 / trace 매칭률 / signal별 top-1 rerank / signal별 risk."""
    total = len(feedbacks)
    signal_dist = Counter(fb["signal"] for fb in feedbacks)
    matched = [fb for fb in feedbacks if fb["trace_id"] in traces]
    match_rate = (len(matched) / total) if total else 0.0

    score_by_signal: dict[str, list[float]] = defaultdict(list)
    risk_by_signal: dict[str, Counter] = defaultdict(Counter)

    for fb in matched:
        rec = traces[fb["trace_id"]]
        scores = (rec.get("retrieval") or {}).get("rerank_scores") or []
        if scores:
            score_by_signal[fb["signal"]].append(float(scores[0]))
        risk = (rec.get("verification") or {}).get("risk_level")
        if risk:
            risk_by_signal[fb["signal"]][risk] += 1

    return {
        "period_total": total,
        "matched_count": len(matched),
        "trace_match_rate": round(match_rate, 3),
        "signal_distribution": dict(signal_dist),
        "top1_score_by_signal": {
            s: round(sum(vs) / len(vs), 4) for s, vs in score_by_signal.items() if vs
        },
        "risk_by_signal": {s: dict(c) for s, c in risk_by_signal.items()},
    }


def _render_feedback(summary: dict[str, Any], days: int) -> None:
    print(f"─── Feedback ({days}d, DB JOIN) ────────────")
    n = summary["period_total"]
    print(f"  total feedback:    {n}")
    print(f"  trace matched:     {summary['matched_count']} ({summary['trace_match_rate']:.1%})  [target ≥ 95%]")
    if n == 0:
        print("  (no feedback collected — endpoint may need promotion)")
        print()
        return

    sig = summary["signal_distribution"]
    parts = " | ".join(f"{s}:{sig.get(s, 0)}" for s in ("up", "down", "reformulated"))
    print(f"  signal:            {parts}")

    if summary["top1_score_by_signal"]:
        scores = " | ".join(f"{s}={v}" for s, v in summary["top1_score_by_signal"].items())
        print(f"  rerank top-1 mean: {scores}")
    if summary["risk_by_signal"]:
        for s, dist in summary["risk_by_signal"].items():
            print(f"  risk[{s}]:          {dist}")
    print()


# --- 메인 ---------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    dirs = resolve_date_dirs(args)
    records: Records = list(load_traces(dirs, args))

    period = _format_period(dirs)
    summary = aggregate(records)
    render(summary, period)

    if args.feedback:
        feedbacks = _load_feedback(args.days)
        traces_by_id = _load_traces_by_id(args.days)
        fb_summary = _aggregate_feedback(feedbacks, traces_by_id)
        _render_feedback(fb_summary, args.days)
        summary["feedback"] = fb_summary

    out_path = save_summary(summary, args, period)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
