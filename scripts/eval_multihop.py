"""멀티홉(에이전트) 골든 채점 — 툴콜링이 여러 홉을 **엮는지** 잰다. (로드맵 B6)

`eval_tool_routing.py` 는 "툴 하나를 맞게 고르나"(1홉)를 쟀다. 여기는 그다음 질문이다 —
**if-elif 로 못 짜는 조합**을 에이전트가 스스로 엮는가. 그게 툴콜링을 도입할 유일한 근거다
(라우팅만 보면 정규식 1.00 을 못 이긴다는 게 이미 실측됐다, eval_tool_routing 참조).

진짜 에이전트 루프다 — 툴 호출을 **실제 SQL 엔드포인트로 디스패치**한다. 스텁이 아니라
`/coverage`·`/payout` 등이 진짜 DB 를 읽고, 그 결과를 모델에 돌려줘 다음 홉을 결정하게 한다.
그래서 이 채점은 "모델이 툴 이름을 잘 고르나"가 아니라 **"실제 데이터로 답까지 엮나"** 를 잰다.

채점 3축 —
  ① 툴 recall    required_tools ⊆ 실제 호출한 툴          (필요한 홉을 다 밟았나)
  ② 금지툴 위반  forbidden_tools ∩ 호출한 툴 = ∅          (부르면 안 되는 걸 불렀나)
  ③ 요소 recall  required_elements 가 최종 답에 다 있나    (완결성 — golden_completeness 와 같은 축)

②가 특히 중요하다. 이 도메인의 사고는 "못 찾음"이 아니라 **"없는 걸 있다고 답함"** 이다:
미보장이 확정인데 지급률을 붙이거나(모순), 없는 담보에 금액을 붙이거나(환각).
그래서 금지툴은 오답보다 무겁게 본다.

**첫 측정 (2026-09-07, Qwen3-4B-AWQ, n=8)** — 결과가 위험의 위치를 바꿨다:

    툴 recall     6/8 = 0.75      홉은 대체로 밟는다
    금지툴 위반   0건             규칙은 지킨다("미보장이면 지급 조회 마라")
    **요소 recall 3/8 = 0.38**    ← 결정론 결과를 **LLM 이 왜곡**한다

실패는 툴 선택이 아니라 **결과 해석**에서 났다:

    MH04  결정론 "C73 → 미보장(암진단비) → 실제 담보: 갑상선암"
          에이전트 "암진단비 담보에 따라 갑상선암(C73)은 보장됩니다"      ← 정반대
    MH03  결정론 "암진단자금 [1년이상] → 가입금액의 100%"
          에이전트 "가입금액의 50%를 지급합니다"                          ← 숫자 왜곡
    MH01  lookup_payout 을 **부르지도 않고** "지급액은 가입금액의 10%"     ← 환각
    MH08  결정론 "Z99 → 판정불가"
          에이전트 "Z99 는 '기타 만성질환'에 해당하며"                     ← 사실 창작

MH04 가 특히 무겁다. 바로 이 세션에서 `/coverage` 의 교차회사 오염을 고쳐 결정론 계층이
정답을 내게 만들었는데, **LLM 이 그 정답을 읽고 뒤집었다.** 소비자한테는 직접 손해다.

→ 함의: **최종 합성을 LLM 에 맡기면 안 된다.** 이건 새 발견이 아니라 이 프로젝트가 이미
   세운 원칙의 실증이다 — eval-and-golden §8 이 `format_answer` 를 **결정론 템플릿**으로
   둔 이유가 정확히 이것이다. 에이전트는 **어느 툴을 어떤 순서로 부를지**(0.75, 위반 0)까지만
   맡기고, 답 문장은 format_* 가 짜는 게 맞다.
   eval_tool_routing(1홉 선택)은 이 위험을 못 봤다 — 멀티홉 골든이라야 드러난다.

전제: 스택 기동(make up) + vLLM 에 `--enable-auto-tool-choice --tool-call-parser hermes`.

용법:
    python3 scripts/eval_multihop.py            # 채점
    python3 scripts/eval_multihop.py --show     # 홉별 툴콜·결과까지
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.eval_tool_routing import TOOLS, MODEL, BASE   # noqa: E402  툴 스키마 재사용

GOLDEN = ROOT / "data/eval/golden_multihop.jsonl"
API = os.environ.get("DOCS_RAG_API", "http://localhost:8002/api/v1/docs-rag")
MAX_HOPS = 5          # 무한루프 방지. 골든 최대 2홉이라 넉넉하다

SYSTEM = (
    "당신은 보험 약관 질의를 도구로 답하는 에이전트다.\n"
    "- 필요한 도구를 순서대로 호출해 답에 필요한 사실을 모두 모아라.\n"
    "- 보장 판정이 '미보장'이면 지급액을 조회하지 마라. 모순된 답이 된다.\n"
    "- 담보가 상품에 없으면 지급액을 조회하지 마라. 없는 담보에 금액을 붙이면 안 된다.\n"
    "- 지급액을 답할 때는 면책 사유도 함께 조회하라.\n"
    "- 사실이 다 모이면 도구 호출을 멈추고 한국어로 답하라."
)

# 툴 이름 → 실제 엔드포인트. 스텁이 아니라 진짜 DB 를 읽는다.
TOOL2EP = {
    "lookup_payout": "payout", "lookup_terms": "terms", "judge_coverage": "coverage",
    "list_coverages": "catalog", "lookup_waiting": "waiting", "lookup_exclusions": "exclusion",
}


def _post(url: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _dispatch(name: str, args: dict, scenario: str) -> str:
    """툴 호출 → 실제 엔드포인트. 결과를 모델이 읽을 문자열로."""
    if name == "search_clauses":
        try:
            d = _post(f"{API}/retrieve", {"query": args.get("query") or scenario,
                                          "service_code": "01", "top_k": 3})
            srcs = d.get("sources") or []
            return "관련 조문:\n" + "\n".join((s.get("content") or "")[:200] for s in srcs[:2]) \
                if srcs else "관련 조문을 찾지 못했습니다."
        except Exception as e:
            return f"검색 실패: {type(e).__name__}"
    ep = TOOL2EP.get(name)
    if not ep:
        return f"알 수 없는 도구: {name}"
    # 인자를 자연어 질의로 합성 — SQL 엔드포인트가 질의 문자열에서 담보·코드·기간을 뽑는다.
    parts = [str(v) for k, v in args.items() if v and k != "product"]
    q = " ".join(parts) or scenario
    if ep in ("payout", "waiting"):
        q += " 얼마"
    if ep == "coverage":
        q += " 보장되나요"
    if ep == "catalog":
        q += " 담보 있어"
    # 브랜드는 원 질의에 있으므로 함께 넘겨 상품 스코프가 걸리게 한다(교차회사 오염 차단)
    q = f"{args.get('product', '')} {q}".strip() if args.get("product") else f"{scenario} / {q}"
    try:
        d = _post(f"{API}/{ep}", {"query": q, "service_code": "01"})
    except Exception as e:
        return f"조회 실패: {type(e).__name__}"
    if not d.get("matched"):
        return "해당 정보를 결정론적으로 확인하지 못했습니다(확인 필요)."
    return str(d.get("answer"))


def _chat(messages: list, timeout: int = 180) -> dict:
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
            "temperature": 0, "max_tokens": 768,
            "chat_template_kwargs": {"enable_thinking": False}}
    return _post(f"{BASE}/chat/completions", body, timeout)["choices"][0]["message"]


def run_agent(scenario: str, show: bool = False) -> tuple[list[str], str]:
    """에이전트 루프. (호출한 툴 순서, 최종 답)."""
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": scenario}]
    called: list[str] = []
    for _ in range(MAX_HOPS):
        m = _chat(msgs)
        tcs = m.get("tool_calls") or []
        if not tcs:
            return called, (m.get("content") or "")
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tcs})
        for tc in tcs:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            called.append(fn["name"])
            result = _dispatch(fn["name"], args, scenario)
            if show:
                print(f"      ↳ {fn['name']}({json.dumps(args, ensure_ascii=False)[:60]})")
                print(f"        → {result[:110]}")
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    return called, "(홉 한도 초과)"


def main() -> int:
    show = "--show" in sys.argv
    rows = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    tool_ok = elem_ok = 0
    violations, tool_miss, elem_miss = [], [], []

    for r in rows:
        print(f"\n  [{r['id']}] {r['scenario'][:52]}   ({r['pattern']})")
        try:
            called, answer = run_agent(r["scenario"], show)
        except Exception as e:
            print(f"      ⚠ {type(e).__name__}: {e}")
            tool_miss.append((r["id"], "ERROR")); elem_miss.append((r["id"], "ERROR"))
            continue
        need = set(r["required_tools"]); got = set(called)
        forbid = set(r["forbidden_tools"]) & got
        missing = need - got
        if not missing:
            tool_ok += 1
        else:
            tool_miss.append((r["id"], ", ".join(sorted(missing))))
        if forbid:
            violations.append((r["id"], ", ".join(sorted(forbid))))
        lack = [e for e in r["required_elements"] if e not in answer]
        if not lack:
            elem_ok += 1
        else:
            elem_miss.append((r["id"], ", ".join(lack)))
        print(f"      호출: {' → '.join(called) or '(없음)'}")
        print(f"      답  : {answer[:110].replace(chr(10), ' ')}")
        print(f"      툴{'✅' if not missing else '❌'} "
              f"요소{'✅' if not lack else '❌'} "
              f"{'🚫금지툴 ' + ','.join(sorted(forbid)) if forbid else ''}")

    n = len(rows)
    print("\n" + "─" * 78)
    print(f"  n={n}   툴 recall {tool_ok}/{n} = {tool_ok / n:.2f}   "
          f"요소 recall {elem_ok}/{n} = {elem_ok / n:.2f}   "
          f"금지툴 위반 {len(violations)}건")
    for label, items in (("툴 누락", tool_miss), ("요소 누락", elem_miss), ("🚫 금지툴", violations)):
        if items:
            print(f"  {label}: " + " / ".join(f"{i}({d})" for i, d in items))
    # 금지툴 위반은 "없는 걸 있다고 답함" — 오답보다 무겁게 본다
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
