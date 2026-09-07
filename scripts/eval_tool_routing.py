"""툴콜링 라우팅 채점 — Qwen3 네이티브 tool call 로 경로 **의도**를 맞히나.

`eval_semantic_route.py` 와 **같은 골든·같은 intent 축**으로 잰다. 그래야 세 방식을
나란히 놓고 비교할 수 있다:

    정규식 (is_*_query)      0ms,   LLM 0회   — eval_sql_routing / eval_semantic_route
    시맨틱 (임베딩 유사도)    ~200ms, LLM 0회   — eval_semantic_route
    툴콜링 (Qwen3)           초 단위, LLM 1회  — 이 스크립트

**왜 재나** — "에이전트로 가는 게 나은가"를 취향이 아니라 숫자로 정하기 위해서다.
툴콜링이 정규식(1.00)을 못 이기면 라우팅 계층에 LLM 을 넣을 이유가 없고, 이기면
멀티홉 조합까지 열린다. 어느 쪽이든 같은 채점기로 확인한다.

**실측 (2026-09-07, Qwen3-4B-AWQ)** — in-sample 과 held-out 을 나눠 쟀다.
held-out(`golden_routing_holdout.jsonl`, n=28)은 실제 DB 담보·상품·조제목으로 만들었고
**어느 라우터의 튜닝에도 쓰지 않았다**.

    방식      정확도(in)  정확도(held)  precision*  오라우팅률(held)  기권  지연 p50
    정규식     1.000**     —             —           —                —     0ms
    시맨틱     0.839       0.714         **0.952**   **0.036**        7     ~200ms
    툴콜링     0.871       0.893         0.893       0.107            0     659ms
    (*precision = 답을 낸 것 중 정답. 기권은 분모 제외 — precision-first 정의 그대로
     **정규식은 최종 route 기준 eval_sql_routing 31/31)

**정확도와 precision 이 반대로 간다.** 툴콜링이 정확도는 높지만(0.893 vs 0.714)
precision 은 낮다(0.893 vs 0.952) — 툴콜링은 **기권을 모르기** 때문이다. 항상 뭔가를
고르니 맞히는 개수는 늘지만, 틀릴 때 확신에 차서 틀린다.

CLAUDE.md 게이트("precision ≥ 0.9 를 독립 평가셋에서")로 판정하면:
    시맨틱 0.952 ✅ 통과 → 메인 경로 투입(SEMANTIC_ROUTE_ENABLED 기본 on)
    툴콜링 0.893 ❌ 미달 → 라우팅 계층에 넣지 않음

지연까지 보면 더 분명하다 — 툴콜링은 659ms·LLM 1회를 쓰고 precision 은 더 나쁘다.
**툴콜링의 값어치는 라우팅이 아니라 멀티홉 조합**(eval_multihop.py)에 있다.

held-out 오라우팅 3건도 성격이 드러난다 — catalog↔coverage("급성심근경색증진단비가
포함돼 있어요?"), waiting↔terms, rag↔coverage. 앞의 둘은 시맨틱이 **기권한** 경계다.

precision-first 채점(시맨틱과 동일): 툴을 하나도 안 고르면 **기권**(오답 아님).
기권하면 기존 결정론 흐름이 받으므로 회귀가 아니다. 진짜 손해는 **오라우팅**이다.

전제: vLLM 이 `--enable-auto-tool-choice --tool-call-parser hermes` 로 떠 있어야 한다.

용법:
    python3 scripts/eval_tool_routing.py            # 채점
    python3 scripts/eval_tool_routing.py --show     # 툴콜 원문까지
"""
import json
import os
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "data/eval/golden_sql_routing.jsonl"


def _golden_path() -> pathlib.Path:
    """--golden <경로> 로 평가셋 교체. held-out 셋 채점용(train-on-test 분리)."""
    if "--golden" in sys.argv:
        return pathlib.Path(sys.argv[sys.argv.index("--golden") + 1])
    return GOLDEN
BASE = os.environ.get("LLM_BASE_URL_HOST", "http://localhost:8000/v1")
MODEL = os.environ.get("LLM_MODEL", "/model")

# 툴 스키마 — `src/v1/rag/semantic_router.py` 의 ROUTE_EXAMPLES 와 **같은 7경로**.
#
# description 이 전부다. LLM 은 함수 코드를 못 보고 이 문장만 보고 고른다. 그래서 지금
# if-elif 순서에 인코딩돼 있는 우선순위 지식(예: "exclusion 은 coverage 뒤")을 여기
# 텍스트로 옮겨야 한다 — 이게 툴콜링이 정규식보다 불리해지는 지점이다.
TOOLS = [
    {"type": "function", "function": {
        "name": "lookup_payout",
        "description": ("보험금을 '얼마' 받는지 조회한다. 지급률·감액·한도 같은 금액. "
                        "'하루 얼마', '몇 퍼센트', '얼마나 깎이나' 류. "
                        "'언제 지급되나'(지급사유 성립 시점)는 여기가 아니라 search_clauses."),
        "parameters": {"type": "object", "properties": {
            "coverage": {"type": "string", "description": "담보명. 예: 중환자실입원급여금"},
            "cause": {"type": "string", "enum": ["질병", "재해"]},
            # enum 으로 못박는다 — 자유 문자열이면 모델이 "2년이상" 같은 **없는 구간**을
            # 만들어내고, select_payout 이 못 알아들어 엉뚱한 행(90일이하)을 고른다(실측).
            "period_bucket": {"type": "string", "description": "가입 후 경과기간 구간",
                              "enum": ["90일이하", "90일초과1년미만", "1년이상"]},
        }, "required": ["coverage"]}}},
    {"type": "function", "function": {
        "name": "lookup_terms",
        "description": ("계약의 '언제까지'를 조회한다 — 청약철회 기간, 갱신 여부·주기, 만기."),
        "parameters": {"type": "object", "properties": {
            "product": {"type": "string", "description": "상품명 또는 브랜드"},
        }, "required": ["product"]}}},
    {"type": "function", "function": {
        "name": "judge_coverage",
        "description": ("질병분류코드(ICD/KCD)나 병명이 보장 범위에 드는지 판정한다. "
                        "'C50 보장되나요', '위암도 되나요' 류. 코드나 구체적 병명이 핵심."),
        "parameters": {"type": "object", "properties": {
            # product 가 없으면 전체 상품에서 코드를 찾아 **교차회사 오염**이 난다
            # (KB 골든라이프 질의가 다이렉트 담보로 리다이렉트되던 실측 사고).
            "product": {"type": "string", "description": "상품명·브랜드. 질의에 있으면 반드시 채운다"},
            "code": {"type": "string", "description": "질병분류코드. 예: C50, D05"},
            "disease": {"type": "string", "description": "병명. 코드가 없을 때만"},
            "coverage": {"type": "string", "description": "판정 대상 담보명. 같은 코드도 담보 따라 갈림"},
        }}}},
    {"type": "function", "function": {
        "name": "list_coverages",
        "description": ("이 상품에 특정 '담보'가 붙어 있는지, 또는 담보 목록을 조회한다. "
                        "'X 담보 있어?', '어떤 담보들이 있나' 류. "
                        "질병코드 판정이 아니라 **담보 멤버십**이 핵심."),
        "parameters": {"type": "object", "properties": {
            "product": {"type": "string"},
            "coverage": {"type": "string", "description": "찾는 담보명"},
        }, "required": ["product"]}}},
    {"type": "function", "function": {
        "name": "lookup_waiting",
        "description": ("가입 후 '언제부터' 온전히 보장되는지 — 면책기간(보장제외기간), "
                        "초기 감액기간. 시간축 질문. 면책 '사유'는 여기가 아니라 lookup_exclusions."),
        "parameters": {"type": "object", "properties": {
            "product": {"type": "string"}, "coverage": {"type": "string"},
        }, "required": ["coverage"]}}},
    {"type": "function", "function": {
        "name": "lookup_exclusions",
        "description": ("보험금을 지급하지 않는 '사유' 목록을 조회한다. "
                        "'뭐가 면책이냐', '어떤 경우에 못 받나' 류. "
                        "특정 상황이 보장되는지 판단하는 건 search_clauses."),
        "parameters": {"type": "object", "properties": {
            "product": {"type": "string"}, "coverage": {"type": "string"},
        }}}},
    {"type": "function", "function": {
        "name": "search_clauses",
        "description": ("약관 조문을 검색해 해석·절차·정의를 답한다. 정해진 값이 아니라 "
                        "조문을 읽어야 하는 질문 — 용어 정의, 청구 절차·서류, "
                        "지급사유가 언제 성립하는지, 특정 상황이 보장되는지."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]}}},
]

# 툴 이름 → 골든의 intent 라벨
TOOL2INTENT = {
    "lookup_payout": "payout", "lookup_terms": "terms", "judge_coverage": "coverage",
    "list_coverages": "catalog", "lookup_waiting": "waiting",
    "lookup_exclusions": "exclusion", "search_clauses": "rag",
}

SYSTEM = ("당신은 보험 약관 질의를 적절한 도구로 라우팅한다. "
          "사용자 질문에 가장 맞는 도구를 **정확히 하나** 호출하라. "
          "확신이 없으면 search_clauses 를 쓴다.")


def _call(query: str, timeout: int = 120) -> tuple[str | None, str]:
    """(선택한 툴 이름, 원문 요약). 툴을 안 고르면 (None, 텍스트)."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": query}],
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": 512,
        # Qwen3 하이브리드 추론 — 라우팅엔 사고가 필요 없고, 켜두면 토큰 한도에 걸려
        # 잘린 <think> 가 새는 사고가 난다(2026-09-07 CRAG 재작성 실측).
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(f"{BASE}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    msg = d["choices"][0]["message"]
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return None, (msg.get("content") or "")[:60].replace("\n", " ")
    fn = tcs[0]["function"]
    return fn["name"], f'{fn["name"]}({fn.get("arguments", "")[:70]})'


def main() -> int:
    show = "--show" in sys.argv
    rows = [json.loads(l) for l in _golden_path().read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r.get("intent")]
    if not rows:
        print(f"골든에 intent 축이 없다: {_golden_path()}", file=sys.stderr)
        return 2

    hit = abstain = wrong = err = 0
    wrongs, abstains = [], []
    print(f"{'질의':<44}{'정답':<10}{'예측':<10}")
    print("-" * 74)
    for r in rows:
        exp = r["intent"]
        try:
            tool, raw = _call(r["query"])
        except Exception as e:
            err += 1
            print(f"{r['query'][:42]:<44}{exp:<10}{'ERR':<10}⚠ {type(e).__name__}")
            continue
        got = TOOL2INTENT.get(tool) if tool else None
        if got is None:
            abstain += 1; abstains.append((r["query"], exp)); mark, pred = "➖", "(기권)"
        elif got == exp:
            hit += 1; mark, pred = "✅", got
        else:
            wrong += 1; wrongs.append((r["query"], exp, got)); mark, pred = "❌", got
        print(f"{r['query'][:42]:<44}{exp:<10}{pred:<10}{mark}" + (f"  {raw}" if show else ""))

    n = len(rows) - err
    print("-" * 74)
    if n:
        print(f"  n={n}  정답 {hit} · 기권 {abstain} · 오라우팅 {wrong}"
              + (f" · 오류 {err}" if err else "")
              + f"   정확도={hit / n:.3f}  오라우팅률={wrong / n:.3f}")
    if wrongs:
        print("  ❌ 오라우팅:")
        for q, e, g in wrongs:
            print(f"     {q[:38]:<40} 정답 {e} ← {g}")
    if abstains:
        print("  ➖ 기권: " + " / ".join(f"{q[:18]}→{e}" for q, e in abstains))
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
