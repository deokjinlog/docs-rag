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

**축(axis)** — 이 프로젝트의 우선순위(구조·중복·표 70%)를 측정 가능한 형태로 나눈 것:
  clause      조문 질의(기존 29문항). 검색 일반 성능
  table_fact  답이 표의 셀에 있는 질의. 표가 선형화로 깨지면 여기가 먼저 떨어진다
  duplicate   같은 사실이 요약표·본문·특약에 중복된 질의
              - dup_kind=gold : 근거가 되는 **본문**을 집었나 (recall 로 잰다)
              - dup_kind=load : 본문이 글자까지 같아 순위로는 구분 불가.
                                → **dup@10**(top-10 중 같은 내용을 담은 청크 수)이 측정 대상.
                                중복 제거가 실제로 컨텍스트를 비웠는지는 이 숫자가 말한다.

**verified** — 사람이 원문과 대조한 항목만 게이트에 쓴다(`verified:true`). 초안(false)은 같이
재고 표에도 찍지만 회귀 판정에서는 뺀다. 라벨이 틀린 문항으로 배포를 막는 게 더 나쁘다.

**벤치 문서 제외** — 코퍼스에는 파서 벤치용으로 빌려온 공고문(B005·D14x)이 색인돼 있다.
약관이 아니라 채점 대상이 아니므로 검색 결과에서 걸러낸 뒤 순위를 매기고, 대신 **몇 번
끼어들었는지(bench 침입)** 를 따로 보고한다 — 실제로 검색을 방해하면 그때 색인에서 뺀다.

용법:
  make eval-retrieval                                  # api 컨테이너 안 (tb_eval_run 에 저장)
  python3 scripts/eval_retrieval.py                    # 호스트 (DB 저장은 건너뜀)
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

# 파서 벤치용으로 빌려온 관악구 공고문. 약관이 아니라 검색 채점 대상이 아니다.
# 지우지 않고 걸러 재는 이유: 색인에서 빼려면 재적재가 필요한데, 아직 **방해하는지 자체가
# 미측정**이다. 걸러 재면서 침입 횟수를 세고, 실제로 방해하면 그때 뺀다(백업 있음).
BENCH_DOCS = {"B005", "D14101", "D14102", "D14103"}

# query_type → 축. 기존 29문항(procedure·simple_fact·interpretation·None)은 전부 clause.
AXES = ("clause", "table_fact", "duplicate")
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




def _axis(row: dict) -> str:
    """골든 1행 → 축. 기존 문항엔 축 표기가 없으므로 clause 로 떨어진다."""
    qt = row.get("query_type")
    return qt if qt in AXES else "clause"


def _dup_hits(sources: list[dict], probe: str, k: int = 10) -> int:
    """top-k 중 같은 사실을 담은 청크 수. 중복 제거가 컨텍스트를 비웠는지의 직접 지표.

    1 이 이상적(정답 한 번). 4 면 LLM 컨텍스트의 40%가 같은 문장으로 채워졌다는 뜻이고,
    그만큼 다른 근거가 밀려난다 — 이 프로젝트가 중복을 1순위로 둔 이유다.
    """
    a = _norm(probe)
    return sum(1 for s in sources[:k] if a in _norm(s.get("content", "")))


def _agg(items: list[dict]) -> dict:
    """hit/rank 리스트 → recall@k · MRR. 빈 리스트면 None(0.0 으로 위장하지 않는다)."""
    n = len(items)
    if not n:
        return {"n": 0, "recall": {}, "mrr": None}
    return {"n": n,
            "recall": {k: sum(i["hit"][k] for i in items) / n for k in KS},
            "mrr": sum((1.0 / i["rank"]) if i["rank"] else 0.0 for i in items) / n}


def _store_run(summary: dict, per_query: list[dict], config: dict) -> str | None:
    """run 을 tb_eval_run/tb_eval_item 에 남긴다. DB 에 못 닿으면 조용히 건너뛴다.

    호스트에서 돌면 sqlalchemy·v1 이 없다 — 그건 실패가 아니라 실행 위치의 차이라서
    채점 자체를 막지 않는다. 대신 저장 여부를 화면에 분명히 적는다(모르는 채로 두지 않는다).
    """
    import os
    import subprocess
    import time
    import uuid

    sys.path.insert(0, str(_HERE.parent / "src"))
    try:
        from sqlalchemy import text

        from v1.config import task_session
    except Exception as e:
        print(f"  ⚠ run 미저장 — DB 계층 없음({type(e).__name__}). "
              f"기록하려면 `make eval-retrieval`(api 컨테이너 안)")
        return None

    sha = os.environ.get("GIT_SHA") or None
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                 text=True, cwd=_HERE.parent).stdout.strip() or None
        except FileNotFoundError:
            sha = None
    run_id = f"retrieval-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    try:
        with task_session() as db:
            db.execute(text("INSERT INTO tb_eval_run (run_id, kind, git_sha, config_json, summary_json) "
                            "VALUES (:r,'retrieval',:g,CAST(:c AS JSONB),CAST(:s AS JSONB))"),
                       {"r": run_id, "g": sha, "c": json.dumps(config, ensure_ascii=False),
                        "s": json.dumps(summary, ensure_ascii=False)})
            for q in per_query:
                db.execute(text("INSERT INTO tb_eval_item (run_id, item_id, scores_json, detail_json) "
                                "VALUES (:r,:i,CAST(:sc AS JSONB),CAST(:d AS JSONB))"),
                           {"r": run_id, "i": q["query"][:255],
                            "sc": json.dumps({"rank": q["rank"], "rerank": q["rerank"],
                                              "dup_at10": q["dup"]}),
                            "d": json.dumps({k: q[k] for k in
                                             ("axis", "verified", "document_id", "gold_clause",
                                              "top_chunk_ids", "bench_intrusion")},
                                            ensure_ascii=False)})
            db.commit()
    except Exception as e:                    # DB 는 있는데 쓰기가 실패 — 채점 결과는 살린다
        print(f"  ⚠ run 저장 실패({type(e).__name__}: {e})")
        return None
    print(f"  run {run_id} (git {sha}) → tb_eval_run")
    return run_id


def main():
    update = "--update-baseline" in sys.argv
    custom = "--golden" in sys.argv                       # 별도 골든(예: 새 코퍼스 초안) 채점
    gpath = pathlib.Path(sys.argv[sys.argv.index("--golden") + 1]) if custom else GOLDEN
    rows = [json.loads(l) for l in open(gpath, encoding="utf-8") if l.strip()]

    print(f"검색 골든셋 채점 — recall@k · MRR  (API {API_BASE}) · {gpath.name}")
    print("=" * 100)
    print(f"{'질의':<38}{'축':<11}{'정답 조':<20}{'순위':<5}{'rerank':<8}{'dup@10':<7}hit@1/3/5")
    print("-" * 100)

    misses = []
    neg_total = neg_pass = 0
    neg_fail: list = []
    per_query: list[dict] = []
    bench_intrusions = 0
    try:
        for r in rows:
            raw = _retrieve(r["query"], r.get("service_code", "01"), r.get("document_id"))
            # 벤치 문서를 빼고 순위를 다시 매긴다 — "색인에 없었다면" 과 같은 순위가 된다.
            src = [s for s in raw if s.get("document_id") not in BENCH_DOCS]
            intruded = len(raw) - len(src)
            bench_intrusions += intruded
            # 부정 케이스(anchor 없이 max_top1만) — "정답이 없어야 하는 질의"의 검색 점수 상한.
            # 긍정 골든이 못 잡는 유형: 도메인 밖인데 리랭커가 높은 점수를 주면 조용한 실패의
            # 온상이 된다(정답 문서가 색인에 없을 때 무관한 청크에 0.99가 붙는 실측 사례).
            # 실측 분포로 임계를 정했다 — 부정 4건 0.0014~0.2182 vs 긍정 p10=0.83. 사이가 비어
            # 있어 0.30(=CRAG_SCORE_THRESHOLD)이 두 분포를 가른다.
            if r.get("anchor") is None and r.get("max_top1") is not None:
                top1 = float(src[0].get("rerank_score") or 0.0) if src else 0.0
                ok = top1 <= float(r["max_top1"])
                neg_total += 1
                neg_pass += 1 if ok else 0
                if not ok:
                    neg_fail.append((r["query"], top1, r["max_top1"]))
                print(f"{r['query'][:36]:<38}{'negative':<11}{'(부정)':<20}{'—':<5}"
                      f"{top1:<8.4f}{'—':<7}{'✅' if ok else '❌'} ≤{r['max_top1']}")
                continue
            rank, rscore = _rank_of_anchor(src, r["anchor"])
            hit = {k: (1 if rank and rank <= k else 0) for k in KS}
            if not rank:
                misses.append(r["query"])
            axis = _axis(r)
            dup = _dup_hits(src, r.get("dup_anchor") or r["anchor"]) if axis == "duplicate" else None
            per_query.append({
                "query": r["query"], "axis": axis, "verified": bool(r.get("verified")),
                "document_id": r.get("document_id"), "gold_clause": r.get("gold_clause"),
                "hit": hit, "rank": rank, "rerank": rscore, "dup": dup,
                "segment": _classify_segment(r["query"]), "bench_intrusion": intruded,
                "top_chunk_ids": [s.get("chunk_id") for s in src[:10]],
            })
            rank_s = str(rank) if rank else "—"
            marks = "".join("✅" if hit[k] else "❌" for k in (1, 3, 5))
            label = (r.get("gold_clause") or r.get("gold_doc") or "")[:18]
            flag = "" if r.get("verified") else " ·초안"
            print(f"{r['query'][:36]:<38}{axis + flag:<11}{label:<20}{rank_s:<5}"
                  f"{rscore:<8.3f}{('—' if dup is None else str(dup)):<7}{marks}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"\n⚠️  스택 미가동 — /retrieve 호출 실패: {e}")
        print("   docker compose up -d 후 색인이 있어야 검색 채점 가능(로드맵 A4).")
        sys.exit(2)

    ver = [q for q in per_query if q["verified"]]
    overall, gated = _agg(per_query), _agg(ver)
    print("-" * 100)
    for name, a in (("전체", overall), ("verified(게이트)", gated)):
        if a["n"]:
            print(f"  {name:<18}" + "  ".join(f"recall@{k}={a['recall'][k]:.3f}" for k in KS)
                  + f"  |  MRR={a['mrr']:.3f}   (n={a['n']})")

    axis_out = {}
    for ax in AXES:
        items = [q for q in per_query if q["axis"] == ax]
        if not items:
            continue
        a = _agg(items)
        dups = [q["dup"] for q in items if q["dup"] is not None]
        a["dup_mean"] = round(sum(dups) / len(dups), 2) if dups else None
        a["dup_max"] = max(dups) if dups else None
        axis_out[ax] = a
        extra = "" if a["dup_mean"] is None else f"  |  dup@10 평균={a['dup_mean']} 최대={a['dup_max']}"
        print(f"    {ax:<12}(n={a['n']:2}) " + "  ".join(f"@{k}={a['recall'][k]:.3f}" for k in KS)
              + f"  MRR={a['mrr']:.3f}{extra}")

    if neg_total:
        print(f"  부정(도메인 밖) {neg_pass}/{neg_total} 통과"
              + ("" if not neg_fail else
                 "  ❌ " + " / ".join(f"{q[:20]} top1={t:.3f}>{m}" for q, t, m in neg_fail)))
    if misses:
        print(f"  top-{TOP_K} 밖 미검출 {len(misses)}건: " + " / ".join(m[:24] for m in misses))
    nq = len([q for q in per_query if q["bench_intrusion"]])
    print(f"  bench 침입: {bench_intrusions}청크 / {nq}문항 "
          + ("(색인에서 뺄 근거 없음 — 걸러 재는 것으로 충분)" if not bench_intrusions
             else "← 검색을 방해하고 있다. 색인 제거 검토(백업 있음)"))

    if custom:                                            # 별도 골든 — baseline 대조 생략(다른 셋)
        print(f"\n  ⚠ 커스텀 골든({gpath.name}) — baseline 대조 생략. 전 코퍼스 검색 = 회사 넘어 시험.")
        return

    # 세그먼트 분해(로드맵 §1.4) — 도메인 어휘 질의가 일반보다 검색 열위인가
    if "--segment" in sys.argv:
        seg_out = {}
        print("-" * 100)
        print("  [세그먼트 분해 — 도메인 어휘 vs 일반]")
        for seg in ("domain", "general"):
            items = [q for q in per_query if q["segment"] == seg]
            if not items:
                continue
            a = _agg(items)
            seg_out[seg] = {"n": a["n"], "recall": a["recall"], "mrr": round(a["mrr"], 3)}
            print(f"    {seg:<8}(n={a['n']:2}) recall "
                  + "  ".join(f"@{k}={a['recall'][k]:.3f}" for k in KS) + f"  MRR={a['mrr']:.3f}")
        d5 = seg_out.get("domain", {}).get("recall", {}).get(5)
        g5 = seg_out.get("general", {}).get("recall", {}).get(5)
        verdict = None
        if d5 is not None and g5 is not None:
            gap = g5 - d5
            verdict = ("도메인 열위(≥0.1)" if gap >= 0.1 else "도메인 열위 없음")
            print(f"    → recall@5 격차(일반−도메인)={gap:+.3f} → {verdict} "
                  f"({'retrieval-bound 신호' if gap >= 0.1 else 'retrieval 병목 배제 견고화'})")
        SEGMENTS.write_text(json.dumps(
            {"vocab": DOMAIN_VOCAB, "segments": seg_out, "verdict": verdict,
             "n": overall["n"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → 저장: {SEGMENTS.name}")

    summary = {"overall": overall, "verified": gated, "axes": axis_out,
               "negatives": {"pass": neg_pass, "total": neg_total},
               "bench_intrusion_chunks": bench_intrusions,
               "misses": misses}
    _store_run(summary, per_query,
               {"golden": gpath.name, "top_k": TOP_K, "bench_excluded": sorted(BENCH_DOCS),
                "api": API_BASE, "gate_scope": "verified"})

    # 순증(monotonic) 대조 — **verified 부분집합**의 recall@5·MRR이 회귀했나.
    # 초안(verified:false)을 게이트에 넣지 않는 이유: 라벨이 틀렸을 수 있는 문항으로 배포를
    # 막으면, 사람이 게이트를 습관적으로 무시하게 된다(V1 에서 같은 판단).
    cur = {"recall": gated["recall"], "mrr": gated["mrr"], "n": gated["n"],
           "axes": {k: {"n": v["n"], "recall": v["recall"], "mrr": round(v["mrr"], 3),
                        "dup_mean": v.get("dup_mean")} for k, v in axis_out.items()}}
    if update:
        prev = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
        cur["note"] = prev.get("note", "")
        BASELINE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ baseline 저장: recall@5={gated['recall'][5]:.3f} MRR={gated['mrr']:.3f} "
              f"(verified n={gated['n']}) → {BASELINE.name}")
        return
    if not BASELINE.exists():
        print("\n⚠️  baseline 없음 — 첫 측정. `--update-baseline`로 기준선 고정 권장.")
        return
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    b5, bm = base["recall"]["5"], base["mrr"]
    reg = []
    if gated["recall"][5] < b5 - EPS:
        reg.append(f"recall@5 {b5:.3f}→{gated['recall'][5]:.3f}")
    if gated["mrr"] < bm - EPS:
        reg.append(f"MRR {bm:.3f}→{gated['mrr']:.3f}")
    print("-" * 100)
    if reg:
        print(f"❌ 검색 회귀(verified n={gated['n']}): {', '.join(reg)} → exit 1")
        sys.exit(1)
    gain = (gated["recall"][5] - b5) + (gated["mrr"] - bm)
    tag = "순증" if gain > EPS else "동률"
    print(f"✅ 무회귀({tag}) — baseline recall@5={b5:.3f} MRR={bm:.3f} 대비 유지/향상")


if __name__ == "__main__":
    main()
