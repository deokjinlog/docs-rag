"""SQL 3경로 라우팅 골든 — /answer가 결정론 질의를 SQL로, 해석/절차/정의를 RAG로 보내나.

게이트가 payout/terms/coverage 3개로 늘어 상호작용 회귀가 실질 위험 → measured 게이트.
채점(스택 필요, /answer 호출):
  · SQL-예상: 200 + route.strategy=="sql" 이어야 pass (SQL 브랜치 조기반환 → vLLM 불필).
  · RAG-예상: **route=sql이 아니면** pass (오라우팅 방지). RAG 경로는 vLLM 없으면 5xx/타임아웃이
    나지만 그건 라우팅 정답(SQL로 안 샘) — route=sql로 200이 오는 것만 fail(hijack).

baseline `routing_sql_baseline.json` 대비 정확도 하락 시 exit 1. `--update-baseline`로 고정.
용법: python3 scripts/eval_sql_routing.py [--update-baseline]
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_sql_routing.jsonl")
BASELINE = os.path.join(HERE, "..", "data", "eval", "routing_sql_baseline.json")
API = os.environ.get("DOCS_RAG_API", "http://localhost:8002/api/v1/docs-rag")


def _route_strategy(query: str) -> tuple[int, str | None]:
    """POST /answer → (status, route.strategy). SQL은 빨리 오고, RAG는 느리거나 5xx."""
    body = json.dumps({"query": query, "service_code": "01"}).encode()
    req = urllib.request.Request(f"{API}/answer", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    # SQL 브랜치는 즉시 반환(<1s). RAG로 가면 vLLM 없을 때 LLM 재시도로 느림 → 짧은 타임아웃으로
    # 끊고 '→RAG'로 판정(오라우팅이면 route=sql로 빨리 왔을 것). vLLM 있으면 넉넉히 늘려도 됨.
    timeout = int(os.environ.get("SQL_ROUTING_TIMEOUT", "15"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return 200, (d.get("route") or {}).get("strategy")
    except urllib.error.HTTPError as e:
        return e.code, None          # 5xx = RAG 경로가 vLLM 없이 실패 (라우팅은 정답일 수 있음)
    except Exception:
        return 0, None               # 타임아웃/연결끊김 — RAG 경로로 갔다는 신호


def main():
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    ok = 0
    print(f"{'질의':<40}{'기대':<7}{'route':<10}판정")
    print("-" * 74)
    for g in rows:
        status, strat = _route_strategy(g["query"])
        is_sql = (status == 200 and strat == "sql")
        hit = is_sql if g["route"] == "sql" else (not is_sql)
        ok += hit
        got = strat if status == 200 else f"HTTP{status}(→RAG)"
        print(f"{g['query'][:38]:<40}{g['route']:<7}{str(got):<10}{'✅' if hit else '❌'}")
        time.sleep(0.2)
    acc = ok / len(rows)
    print("-" * 74)
    print(f"라우팅 정확도 {ok}/{len(rows)} = {acc:.3f}")

    result = {"accuracy": round(acc, 4), "n": len(rows)}
    if "--update-baseline" in sys.argv:
        result["note"] = "SQL 3경로(payout/terms/coverage) vs RAG 라우팅. SQL=route=sql, RAG=route≠sql."
        json.dump(result, open(BASELINE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"✅ baseline 갱신: accuracy={acc:.3f}")
        return
    if os.path.exists(BASELINE):
        base = json.load(open(BASELINE, encoding="utf-8"))
        if acc < base["accuracy"] - 1e-9:
            print(f"❌ 라우팅 회귀: {acc:.3f} < baseline {base['accuracy']:.3f}")
            sys.exit(1)
        print(f"✅ 무회귀 (baseline {base['accuracy']:.3f})")
    else:
        print("(baseline 없음 — --update-baseline로 생성)")


if __name__ == "__main__":
    main()
