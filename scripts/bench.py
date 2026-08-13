"""서빙 지연 벤치 — 3경로 아키텍처의 payoff 정량화.

"얼마·언제·보장·면책"은 SQL 경로가 **LLM 없이 결정론 즉답**(ms), "해석·절차"는 RAG가 검색+
리랭킹(+LLM 생성)로 느리다. 엔드포인트별 지연 분포(p50/p95/mean)를 재 이 차이를 숫자로 남긴다.

- SQL 경로(/payout·/terms·/coverage·/exclusion) + SQL-라우팅 /answer: vLLM 불필(빠름).
- /retrieve: 하이브리드 검색 + CrossEncoder 리랭킹(LLM 없음) — RAG의 '검색 비용' 기준선.
- RAG /answer(LLM 생성)는 vLLM 필요 → BENCH_INCLUDE_RAG=1 일 때만(그 위에 생성 지연이 얹힘).

결과는 data/bench/<날짜>/results.json 에 보존(측정-먼저). 날짜는 --date 로 주입(스크립트가
시계를 직접 안 읽음 → 재현 가능).

용법:
  python3 scripts/bench.py --date 20260813 [--n 20]              # 단일요청 지연
  python3 scripts/bench.py --load --date 20260813 [--dur 3]      # 부하·동시성 스윕(QPS·부하 하 지연)
                          [--path /payout] [--levels 1,2,4,8,16,32]
--load는 SQL 경로에 동시성을 걸어 처리량(QPS)과 부하 하 지연 꼬리를 잰다 → data/bench/<날짜>/load.json.
"""
import os
import sys
import json
import time
import threading
import subprocess
import statistics
import urllib.request
import urllib.error

HERE = os.path.dirname(__file__)
API = os.environ.get("DOCS_RAG_API", "http://localhost:8002/api/v1/docs-rag")


def _detect_mode() -> dict:
    """실행 컨텍스트(모드)를 스스로 기록 — retrieve 지연 해석에 필수.

    lite 모드는 GPU를 비워(다른 작업 공존) 임베더·리랭커를 CPU로 돌린다
    (CUDA_VISIBLE_DEVICES=-1) → retrieve가 초 단위. full/GPU면 sub-초. **SQL 경로는
    임베더·리랭커·LLM을 안 건드려 이 모드와 무관하게 ms.** 그 불변성을 드러내려 기록.
    """
    ctx = {"embed_rerank_device": "unknown", "vllm_up": None}
    try:
        env = subprocess.run(["docker", "exec", "docs-rag-api", "env"],
                             capture_output=True, text=True, timeout=8).stdout
        cvd = next((l.split("=", 1)[1] for l in env.splitlines()
                    if l.startswith("CUDA_VISIBLE_DEVICES=")), "")
        ctx["embed_rerank_device"] = "cpu" if cvd.strip() in ("-1", "") else f"gpu({cvd})"
    except Exception:
        pass
    try:
        names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                               capture_output=True, text=True, timeout=8).stdout
        ctx["vllm_up"] = any("vllm" in n for n in names.splitlines())
    except Exception:
        pass
    return ctx


def _post(path: str, body: dict, timeout: int = 30) -> tuple[float, int]:
    """(elapsed_ms, status). 예외/타임아웃은 status=0."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{API}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return (time.perf_counter() - t0) * 1000, 200
    except urllib.error.HTTPError as e:
        return (time.perf_counter() - t0) * 1000, e.code
    except Exception:
        return (time.perf_counter() - t0) * 1000, 0


# (라벨, path, body, 기대 route) — SQL은 결정론(LLM 없음), retrieve는 검색비용, rag는 LLM 생성
CASES = [
    ("payout   (SQL)",   "/payout",   {"query": "중환자실 입원하면 하루 얼마 받아요?", "service_code": "01"}, "sql"),
    ("terms    (SQL)",   "/terms",    {"query": "중환자실 특약 청약철회 언제까지?", "service_code": "01"}, "sql"),
    ("coverage (SQL)",   "/coverage", {"query": "C50 유방암은 보장되나요?", "service_code": "01"}, "sql"),
    ("exclusion(SQL)",   "/exclusion", {"query": "중환자실 특약은 뭐가 면책?", "service_code": "01"}, "sql"),
    ("answer→SQL",       "/answer",   {"query": "중환자실 하루 얼마?", "service_code": "01"}, "sql"),
    ("retrieve(검색+rerank)", "/retrieve", {"query": "충치는 어떻게 정의되나요?", "service_code": "01", "top_k": 3}, "rag"),
]
RAG_CASE = ("answer→RAG(+LLM)", "/answer", {"query": "무면허운전 시 보장되나요?", "service_code": "01"}, "rag")


def _measure(path: str, body: dict, n: int, timeout: int) -> dict:
    lat = []
    for _ in range(n):
        ms, st = _post(path, body, timeout)
        if st == 200:
            lat.append(ms)
    if not lat:
        return {"n": 0, "note": "모든 호출 실패(비-200)"}
    lat.sort()
    return {
        "n": len(lat),
        "p50_ms": round(statistics.median(lat), 1),
        "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
        "mean_ms": round(statistics.mean(lat), 1),
        "min_ms": round(lat[0], 1),
    }


def _pct(sorted_lat: list, q: float):
    if not sorted_lat:
        return None
    return round(sorted_lat[min(len(sorted_lat) - 1, int(len(sorted_lat) * q))], 1)


def _load_level(path: str, body: dict, concurrency: int, duration_s: float, timeout: int) -> dict:
    """닫힌-루프(closed-loop) 부하 — concurrency개 스레드가 마감까지 요청을 연속 발사.

    처리량(QPS=성공/벽시간)과 **부하 하** 지연분포를 낸다. 단일요청 지연(_measure)과 달리
    동시성이 커질 때 지연이 어떻게 벌어지는지(꼬리)와 포화점을 드러낸다.
    """
    lat, counts, lock = [], {"ok": 0, "err": 0}, threading.Lock()
    deadline = time.monotonic() + duration_s

    def worker():
        local, ok, err = [], 0, 0
        while time.monotonic() < deadline:
            ms, st = _post(path, body, timeout)
            if st == 200:
                ok += 1
                local.append(ms)
            else:
                err += 1
        with lock:
            lat.extend(local)
            counts["ok"] += ok
            counts["err"] += err

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - t0
    lat.sort()
    return {
        "concurrency": concurrency,
        "ok": counts["ok"], "err": counts["err"],
        "qps": round(counts["ok"] / wall, 1) if wall > 0 else 0,
        "p50_ms": _pct(lat, 0.50), "p95_ms": _pct(lat, 0.95), "p99_ms": _pct(lat, 0.99),
        "wall_s": round(wall, 2),
    }


def load_main():
    """부하·동시성 스윕 — SQL 경로가 동시성에서 얼마나 버티나(QPS·부하 하 지연 꼬리)."""
    date = _arg("--date", None)
    if not date:
        print("ERROR: --date YYYYMMDD 필요", file=sys.stderr)
        sys.exit(2)
    path = _arg("--path", "/payout")   # 기본 = 가장 무거운 SQL(payout_rule + 면책 강제첨부 JOIN)
    dur = float(_arg("--dur", "3"))
    levels = [int(x) for x in _arg("--levels", "1,2,4,8,16,32").split(",")]
    body = {"query": "중환자실 입원하면 하루 얼마 받아요?", "service_code": "01"}

    mode = _detect_mode()
    print(f"[부하·동시성 벤치 · {path} · 레벨당 {dur}s · API={API}]")
    print(f"  모드: 임베더·리랭커={mode['embed_rerank_device']} (SQL 경로는 순수 Postgres — GPU 무관)")
    print(f"{'동시성':>6}{'QPS':>10}{'p50':>9}{'p95':>9}{'p99':>9}{'err':>7}  (ms)")
    print("-" * 58)
    rows = []
    for c in levels:
        r = _load_level(path, body, c, dur, timeout=30)
        rows.append(r)
        print(f"{c:>6}{r['qps']:>10}{r['p50_ms']:>9}{r['p95_ms']:>9}{r['p99_ms']:>9}{r['err']:>7}")
    print("-" * 58)
    peak = max(rows, key=lambda r: r["qps"])
    print(f"피크 처리량 ≈ {peak['qps']} QPS @ 동시성 {peak['concurrency']} "
          f"(p95 {peak['p95_ms']}ms) — 결정론 SQL 계층의 단일-노드 처리량")

    out_dir = os.path.join(HERE, "..", "data", "bench", date)
    os.makedirs(out_dir, exist_ok=True)
    out = {"date": date, "api": API, "path": path, "duration_s": dur, "mode": mode, "levels": rows}
    with open(os.path.join(out_dir, "load.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"→ 저장: data/bench/{date}/load.json")


def main():
    if "--load" in sys.argv:
        return load_main()
    n = int(_arg("--n", "20"))
    n_rag = int(_arg("--n-rag", "5"))   # retrieve는 CPU모드서 초 단위 → 반복 분리(낭비 방지)
    date = _arg("--date", None)
    if not date:
        print("ERROR: --date YYYYMMDD 필요(시계 미사용, 재현성)", file=sys.stderr)
        sys.exit(2)

    cases = list(CASES)
    if os.environ.get("BENCH_INCLUDE_RAG") == "1":
        cases.append(RAG_CASE)

    mode = _detect_mode()
    dev = mode["embed_rerank_device"]
    print(f"[서빙 지연 벤치 · API={API}]")
    print(f"  모드: 임베더·리랭커={dev} · vLLM={'up' if mode['vllm_up'] else 'down'} "
          f"(SQL 경로는 이 모드와 무관 — 임베더·리랭커·LLM 미사용)")
    print(f"{'케이스':<22}{'p50':>9}{'p95':>9}{'mean':>9}{'min':>9}  (ms)")
    print("-" * 64)
    results = {}
    for label, path, body, kind in cases:
        iters = n_rag if kind == "rag" else n
        m = _measure(path, body, iters, timeout=120 if kind == "rag" else 30)
        results[label] = {**m, "path": path, "kind": kind}
        if m["n"]:
            print(f"{label:<22}{m['p50_ms']:>9}{m['p95_ms']:>9}{m['mean_ms']:>9}{m['min_ms']:>9}")
        else:
            print(f"{label:<22}{'—':>9}  {m['note']}")
    print("-" * 64)

    # payoff 요약 — SQL 결정론 vs 검색비용(모드 명시)
    sql = [r["p50_ms"] for r in results.values() if r.get("kind") == "sql" and r.get("n")]
    ret = results.get("retrieve(검색+rerank)", {}).get("p50_ms")
    if sql:
        sql_med = round(statistics.median(sql), 1)
        line = f"SQL 경로 결정론 p50 중앙값 ≈ {sql_med}ms (임베더·리랭커·LLM 미사용 → GPU 유무 무관)"
        if ret:
            line += (f" · RAG 검색floor(retrieve, {dev}) p50 ≈ {ret}ms "
                     f"→ {round(ret/max(sql_med,0.1),1)}x (+RAG는 LLM 생성 지연이 추가)")
        print(line)

    out_dir = os.path.join(HERE, "..", "data", "bench", date)
    os.makedirs(out_dir, exist_ok=True)
    out = {"n": n, "n_rag": n_rag, "date": date, "api": API, "mode": mode, "results": results}
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"→ 저장: data/bench/{date}/results.json")


def _arg(flag: str, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


if __name__ == "__main__":
    main()
