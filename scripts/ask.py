"""가벼운 질의 CLI — Swagger/curl 없이 한 줄로 '답 잘 나오나' 검증.

    uv run python scripts/ask.py "중환자실 하루 얼마?"
    uv run python scripts/ask.py "충치는 어떻게 정의되나요?"

/answer가 결정론 질의는 SQL 경로로(LLM 없이 즉답), 해석·절차는 RAG로 보낸다. 이 도구는 그
라우팅·답·근거를 사람이 읽게 편다. RAG 생성은 vLLM 필요 — 없으면(lite 모드) 검색 근거(조)만
보여주고 그렇게 표시한다(없는 걸 있다고 안 함). 순수 stdlib(urllib) — 의존성 0.
"""
import sys
import json
import time
import urllib.request
import urllib.error

API = "http://localhost:8002/api/v1/docs-rag"
DIM, BOLD, RST = "\033[2m", "\033[1m", "\033[0m"


def _post(path, body, timeout):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{API}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _vllm_up():
    """vLLM 생성 서버가 떠 있나(빠른 프로브). 없으면 RAG 생성은 못 하니 /answer 타임아웃을
    짧게 잡아 검색 근거로 빨리 폴백한다(lite 모드 UX). SQL 경로는 어차피 즉답이라 무관."""
    try:
        urllib.request.urlopen("http://localhost:8000/health", timeout=2)
        return True
    except Exception:
        return False


ROUTE_KO = {"payout": "결정론 SQL · 지급(얼마)", "terms": "결정론 SQL · 계약조건(언제)",
            "coverage": "결정론 SQL · 보장판정", "exclusion": "결정론 SQL · 면책",
            "sql": "결정론 SQL"}


def _fmt_sources(sources, n=4):
    out = []
    for i, s in enumerate(sources[:n], 1):
        doc = s.get("document_id", "?")
        hp = s.get("heading_path") or s.get("heading") or (s.get("content", "")[:34])
        if isinstance(hp, list):
            hp = " > ".join(hp[-2:])
        rr = s.get("rerank_score")
        rr_s = f" · rerank {rr:.3f}" if isinstance(rr, (int, float)) else ""
        out.append(f"  {i}. [{doc}] {str(hp)[:52]}{rr_s}")
    return out


def main():
    if len(sys.argv) < 2:
        print("용법: uv run python scripts/ask.py \"질문\"")
        sys.exit(2)
    query = " ".join(sys.argv[1:])
    print(f"\n{BOLD}질문{RST}  {query}")

    # 1) /answer — 라우팅. SQL 경로는 즉답, RAG는 vLLM 필요(없으면 타임아웃 → 근거 폴백).
    #    vLLM 없으면 짧은 타임아웃으로 빠르게 근거 폴백(SQL은 <1s라 안 걸림).
    ans_timeout = 60 if _vllm_up() else 7
    t0 = time.perf_counter()
    try:
        r = _post("/answer", {"query": query, "service_code": "01"}, timeout=ans_timeout)
        ms = (time.perf_counter() - t0) * 1000
        route = (r.get("route") or {})
        strat = route.get("strategy", "?")
        if strat == "sql":
            kind = route.get("query_type") or "sql"
            print(f"{BOLD}인식{RST}  {ROUTE_KO.get(kind, ROUTE_KO['sql'])}  {DIM}(LLM 미사용 · {ms:.0f}ms){RST}")
            print(f"\n{BOLD}── 답 ──{RST}")
            for line in (r.get("answer") or "").split("  ※"):
                print("  " + line.strip().replace("※", "※"))
            print(f"\n{DIM}  route=sql — 관계형 테이블에서 결정론으로 뽑음(검색·생성 안 씀).{RST}")
        else:
            print(f"{BOLD}인식{RST}  RAG 검색·생성 · {route.get('query_type','?')}  {DIM}({ms:.0f}ms){RST}")
            print(f"\n{BOLD}── 생성 답 ──{RST}")
            print("  " + (r.get("answer") or "(빈 답)").replace("\n", "\n  "))
            if r.get("sources"):
                print(f"\n{BOLD}── 근거 (검색된 조) ──{RST}")
                print("\n".join(_fmt_sources(r["sources"])))
            v = r.get("verification") or {}
            if v.get("warnings"):
                print(f"\n{DIM}  검증: {v.get('risk_level','')} · {v['warnings'][0][:60]}{RST}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, Exception) as e:
        # vLLM 없어서 RAG 생성이 안 돎 → 검색 근거만 (없는 걸 있다고 안 함)
        print(f"{BOLD}인식{RST}  RAG 경로 {DIM}(생성=vLLM 필요, 지금 없음 → 검색 근거만){RST}")
        try:
            rr = _post("/retrieve", {"query": query, "service_code": "01", "top_k": 4}, timeout=60)
            print(f"\n{BOLD}── 검색된 근거 (top-{len(rr.get('sources',[]))} 조) ──{RST}")
            print("\n".join(_fmt_sources(rr.get("sources", []))))
            print(f"\n{DIM}  생성 답을 보려면 vLLM 필요: `make answer` 후 재시도.{RST}")
        except Exception as e2:
            print(f"  ⚠ 검색도 실패: {e2}")
            print(f"  {DIM}스택 확인: docker compose ps · make lite{RST}")
    print(f"\n{DIM}이 도구는 라우팅·답·근거만 편다. Swagger: http://localhost:8002/docs{RST}\n")


if __name__ == "__main__":
    main()
