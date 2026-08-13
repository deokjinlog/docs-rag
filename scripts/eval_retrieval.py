"""검색 골든셋 채점 — recall@k · MRR (로드맵 A4, 스택 필요).

관계형 추출 골든(scripts/*.py, make check)은 스택 없이 도는 자립 검증이지만, **검색은
실제 색인(Qdrant)과 리랭커가 있어야** 채점된다. 그래서 이건 make check가 아니라 별도
게이트(smoke_test·eval_ragas와 같은 계열).

정답 정의 — **원문 verbatim 앵커**: 각 질의의 정답 조에만 나오는 구절을 골든에 박고,
/retrieve top_k 결과 중 **아무 청크의 본문에 그 앵커가 있으면 hit**. chunk_id가 아니라
본문 구절로 판정하므로 재색인·재청킹(청크 id가 바뀜)에도 안 깨진다.

지표:
- recall@k = 질의당 hit@k(정답 앵커가 top-k 안에 있나)의 평균. k ∈ {1,3,5,10}.
- MRR     = 1/(첫 정답 순위)의 평균. 순위 감도(상위에 얼마나 잘 올렸나)를 본다.

순증(monotonic) — 청킹·프리페치·리랭커를 바꾼 뒤 이걸 돌려 recall@5·MRR이 baseline보다
떨어지면 ❌(회귀)로 exit 1. baseline 갱신은 --update-baseline.

용법:
  python3 scripts/eval_retrieval.py                 # 채점 + baseline 대조
  python3 scripts/eval_retrieval.py --update-baseline  # 현재 수치를 baseline으로 저장
  RAG_API_BASE=http://host:8002/api/v1/docs-rag python3 scripts/eval_retrieval.py
"""
import os
import re
import sys
import json
import pathlib
import unicodedata
import urllib.request
import urllib.error
import http.client

_HERE = pathlib.Path(__file__).parent
GOLDEN = _HERE.parent / "data" / "eval" / "golden_retrieval.jsonl"
BASELINE = _HERE.parent / "data" / "eval" / "retrieval_baseline.json"
SEGMENTS = _HERE.parent / "data" / "eval" / "retrieval_segments.json"
API_BASE = os.environ.get("RAG_API_BASE", "http://localhost:8002/api/v1/docs-rag")
KS = [1, 3, 5, 10]
TOP_K = max(KS)
EPS = 1e-6                                   # baseline 대조 허용오차(부동소수 노이즈)

# 세그먼트 분류(로드맵 §1.4) — 약관 **도메인 어휘/엔티티**를 담은 질의 vs 일반 소비자 질의.
# 파인튜닝 판별식이 묻는 것: 일반 임베더가 도메인 용어를 일반어만큼 검색하나(도메인 열위면
# retrieval-bound → Phase 1). 목록을 **공개**해 세그먼트가 재현·감사 가능하게(체리피킹 방지).
DOMAIN_VOCAB = [
    # 담보·특약·질병 고유명(엔티티)
    "소득보장수술", "중환자실", "간병인", "입원급여금", "입원비보험", "충치", "치아우식증",
    # 약관 계약 전문용어
    "특약", "준용", "갱신", "해약환급금", "감액", "연체", "청약철회", "면책", "담보",
]


def _classify_segment(query: str) -> str:
    """질의가 도메인 어휘를 담으면 'domain', 아니면 'general'. 공개 목록 기반(재현 가능)."""
    return "domain" if any(v in query for v in DOMAIN_VOCAB) else "general"


def _norm(s: str) -> str:
    """채점 정규화 — 공백·구두점·유니코드 표기차 흡수(golden_eval._norm과 동일 규약).
    '가입 후 1 년간 보험금 50%' 와 앵커 '가입 후 1 년간 보험금 50' 을 같은 축으로."""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[,.·“”\"'()\[\]%]", "", s)
    return s.lower()


def _wait_api(max_wait: int = 90) -> bool:
    """API가 200 줄 때까지 대기(최대 max_wait초). 8GB 박스에서 무거운 쿼리가
    API 컨테이너를 OOM-kill → unless-stopped로 자동 재시작되는 주기를 견디기 위함."""
    import time
    probe = f"{API_BASE}/documents/01/R04"
    for _ in range(max_wait // 3):
        try:
            with urllib.request.urlopen(probe, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _retrieve(query: str, service_code: str, document_id: str | None, retries: int = 3) -> list[dict]:
    """POST /retrieve → sources 리스트(rerank 정렬). 연결 끊김(무거운 쿼리 OOM-restart)이면
    API 복구를 기다렸다가 재시도. retries 소진 후에도 실패면 예외를 위로 던진다(진짜 다운)."""
    import time
    body = {"query": query, "service_code": service_code, "top_k": TOP_K}
    if document_id:
        body["document_id"] = document_id
    payload = json.dumps(body).encode("utf-8")
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            f"{API_BASE}/retrieve", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:  # dense_heavy는 CPU 리랭커가 느림(vLLM과 경합)
                return json.loads(r.read().decode("utf-8")).get("sources", [])
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.RemoteDisconnected) as e:
            if attempt == retries:
                raise
            print(f"    ↻ 연결 끊김({type(e).__name__}) — API 복구 대기 후 재시도 {attempt}/{retries-1}")
            _wait_api()
            time.sleep(2)


def _rank_of_anchor(sources: list[dict], anchor: str) -> tuple[int, float]:
    """정답 앵커가 처음 등장하는 순위(1-index)와 그 청크 rerank score. 없으면 (0, 0.0)."""
    a = _norm(anchor)
    for i, s in enumerate(sources, 1):
        if a in _norm(s.get("content", "")):
            return i, float(s.get("rerank_score") or 0.0)
    return 0, 0.0


def main():
    update = "--update-baseline" in sys.argv
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]

    print(f"검색 골든셋 채점 — recall@k · MRR  (API {API_BASE})")
    print("=" * 92)
    print(f"{'질의':<40}{'정답 조':<22}{'순위':<5}{'rerank':<8}hit@1/3/5")
    print("-" * 92)

    agg = {k: 0 for k in KS}
    mrr = 0.0
    misses = []
    per_query = []          # 세그먼트 집계용 (segment, hit@k, rank)
    try:
        for r in rows:
            src = _retrieve(r["query"], r.get("service_code", "01"), r.get("document_id"))
            rank, rscore = _rank_of_anchor(src, r["anchor"])
            hit = {k: (1 if rank and rank <= k else 0) for k in KS}
            for k in KS:
                agg[k] += hit[k]
            mrr += (1.0 / rank) if rank else 0.0
            if not rank:
                misses.append(r["query"])
            per_query.append({"segment": _classify_segment(r["query"]), "hit": hit, "rank": rank})
            rank_s = str(rank) if rank else "—"
            marks = "".join("✅" if hit[k] else "❌" for k in (1, 3, 5))
            print(f"{r['query'][:38]:<40}{r['gold_clause'][:20]:<22}{rank_s:<5}"
                  f"{rscore:<8.3f}{marks}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"\n⚠️  스택 미가동 — /retrieve 호출 실패: {e}")
        print("   docker compose up -d 후 색인이 있어야 검색 채점 가능(로드맵 A4).")
        sys.exit(2)

    n = len(rows)
    recall = {k: agg[k] / n for k in KS}
    mrr /= n
    print("-" * 92)
    line = "  ".join(f"recall@{k}={recall[k]:.3f}" for k in KS) + f"  |  MRR={mrr:.3f}"
    print(f"  {line}   (n={n})")
    if misses:
        print(f"  top-{TOP_K} 밖 미검출 {len(misses)}건: " + " / ".join(m[:24] for m in misses))

    # 세그먼트 분해(로드맵 §1.4) — 도메인 어휘 질의가 일반보다 검색 열위인가
    if "--segment" in sys.argv:
        seg_out = {}
        print("-" * 92)
        print("  [세그먼트 분해 — 도메인 어휘 vs 일반]")
        for seg in ("domain", "general"):
            items = [q for q in per_query if q["segment"] == seg]
            if not items:
                continue
            sn = len(items)
            srecall = {k: sum(q["hit"][k] for q in items) / sn for k in KS}
            smrr = sum((1.0 / q["rank"]) if q["rank"] else 0.0 for q in items) / sn
            seg_out[seg] = {"n": sn, "recall": srecall, "mrr": round(smrr, 3)}
            sline = "  ".join(f"@{k}={srecall[k]:.3f}" for k in KS)
            print(f"    {seg:<8}(n={sn:2}) recall {sline}  MRR={smrr:.3f}")
        # 판정 보조: 도메인이 일반보다 recall@5 유의미 열위인가
        d5 = seg_out.get("domain", {}).get("recall", {}).get(5)
        g5 = seg_out.get("general", {}).get("recall", {}).get(5)
        verdict = None
        if d5 is not None and g5 is not None:
            gap = g5 - d5
            verdict = ("도메인 열위(≥0.1)" if gap >= 0.1 else "도메인 열위 없음")
            print(f"    → recall@5 격차(일반−도메인)={gap:+.3f} → {verdict} "
                  f"({'retrieval-bound 신호' if gap >= 0.1 else 'retrieval 병목 배제 견고화'})")
        SEGMENTS.write_text(json.dumps(
            {"vocab": DOMAIN_VOCAB, "segments": seg_out, "verdict": verdict, "n": n},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → 저장: {SEGMENTS.name}")

    # 순증(monotonic) 대조 — recall@5·MRR이 회귀했나
    cur = {"recall": recall, "mrr": mrr, "n": n}
    if update:
        BASELINE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ baseline 저장: recall@5={recall[5]:.3f} MRR={mrr:.3f} → {BASELINE.name}")
        return
    if not BASELINE.exists():
        print(f"\n⚠️  baseline 없음 — 첫 측정. `--update-baseline`로 기준선 고정 권장.")
        return
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    b5, bm = base["recall"]["5"], base["mrr"]
    reg = []
    if recall[5] < b5 - EPS:
        reg.append(f"recall@5 {b5:.3f}→{recall[5]:.3f}")
    if mrr < bm - EPS:
        reg.append(f"MRR {bm:.3f}→{mrr:.3f}")
    print("-" * 92)
    if reg:
        print(f"❌ 검색 회귀: {', '.join(reg)} → exit 1")
        sys.exit(1)
    gain = (recall[5] - b5) + (mrr - bm)
    tag = "순증" if gain > EPS else "동률"
    print(f"✅ 무회귀({tag}) — baseline recall@5={b5:.3f} MRR={bm:.3f} 대비 유지/향상")


if __name__ == "__main__":
    main()
