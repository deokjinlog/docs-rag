"""멀티홉(에이전트) 골든 채점 — 툴콜링이 여러 홉을 **엮는지** 잰다. (로드맵 B6)

`eval_tool_routing.py` 는 "툴 하나를 맞게 고르나"(1홉)를 쟀다. 여기는 그다음 질문이다 —
**if-elif 로 못 짜는 조합**을 에이전트가 스스로 엮는가. 그게 툴콜링을 도입할 유일한 근거다
(라우팅만 보면 정규식 1.00 을 못 이긴다는 게 이미 실측됐다, eval_tool_routing 참조).

진짜 에이전트 루프다 — 툴 호출을 **실제 SQL 엔드포인트로 디스패치**한다. 스텁이 아니라
`/coverage`·`/payout` 등이 진짜 DB 를 읽고, 그 결과를 모델에 돌려줘 다음 홉을 결정하게 한다.
그래서 이 채점은 "모델이 툴 이름을 잘 고르나"가 아니라 **"실제 데이터로 답까지 엮나"** 를 잰다.

채점 4축 —
  ① 툴 recall    required_tools ⊆ 실제 호출한 툴          (필요한 홉을 다 밟았나)
  ② 금지툴 위반  forbidden_tools ∩ 호출한 툴 = ∅          (부르면 안 되는 걸 불렀나)
  ③ 요소 recall  required_elements 가 최종 답에 다 있나    (완결성 — golden_completeness 와 같은 축)
  ④ **금지문구**  must_not_contain 이 답에 없나            (있으면 안 되는 말을 했나)

④가 이 도메인의 진짜 안전장치다. ③(요소 recall)은 "빠뜨렸나"만 보는데, 실제 사고는
**뒤집어 말하는 것**이었다 — 결정론이 "미보장"을 냈는데 답이 "보장됩니다"가 되면 필수
요소는 다 들어 있어도 소비자는 정반대 정보를 받는다. 금액도 같다: 갑상선암진단자금(10%)
질의에 "50%"(암진단자금)가 섞이면 5배 오답이다. ③으로는 둘 다 못 잡는다.

②가 특히 중요하다. 이 도메인의 사고는 "못 찾음"이 아니라 **"없는 걸 있다고 답함"** 이다:
미보장이 확정인데 지급률을 붙이거나(모순), 없는 담보에 금액을 붙이거나(환각).
그래서 금지툴은 오답보다 무겁게 본다.

**측정 (2026-09-08, Qwen3-4B-AWQ, n=12)** — 최종 합성만 바꾼 A/B:

    최종 합성        툴 recall   요소 recall   금지문구   금지툴 위반
    결정론 조립       1.00        **0.92**      0.92       0
    LLM 재서술        1.00        **0.67**      0.92       0

**툴 recall 이 같다**는 게 핵심이다 — 에이전트가 밟은 홉도, 손에 쥔 사실도 같은데
답 문장을 LLM 이 쓰면 요소의 4분의 1이 더 날아간다. 역할 분담이 숫자로 정해진다:
**에이전트는 "어느 툴을 어떤 순서로"까지, 답 문장은 결정론 템플릿.**
새 원칙이 아니라 eval-and-golden §8 이 format_answer 를 결정론 템플릿으로 둔 이유의 실증.

**남은 실패 2건은 둘 다 모델의 인자 채우기 실패**다(골든이 맞고 모델이 틀렸다):
    MH12  product 에 담보명을, coverage 에 "수술급여금"(소득보장 누락)을 넣어 미매칭
    MH02  질의에 없는 "중환자실입원급여금" 을 coverage 로 **지어내** 무관한 담보 값이 섞임
          → 금지문구 축이 이걸 잡는다. 툴 recall 은 required ⊆ called 이라 **여분 호출**을
            못 잡는데, 금지문구가 그 구멍을 메운다.

**채점기도 두 번 고쳤다 — 골든이 자기 채점기의 결함도 드러냈다:**
  ① 요소를 substring 정확 일치로만 보면 **표현 차이가 사실 오류로 오분류**된다. 실측:
     "암진단비가 보장되지 않습니다" 는 판정이 정확히 맞는데 '미보장' 단어가 없어 실패로
     셌다. → 요소 항목이 리스트면 **any-of(동의 표현)**. 이걸 넣자 결정론 0.92 로 올랐다.
     그 전 0.42 는 LLM 의 어휘 취향을 재고 있었다.
  ② 금지문구는 **그 답에서만 틀린 신호**여야 한다. "50%" 를 금지했다가 중환자실 감액
     문구("1년이내 재해외 시 50% 감액")에 걸려 오탐했다. 숫자처럼 다의적인 토큰은
     부적합 → "중환자실"(무관한 담보 혼입) 로 교정.

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
    # 인자 → 자연어 질의 합성. SQL 엔드포인트가 질의 **문자열**에서 담보·코드·기간을 뽑으므로
    # 슬롯을 아무렇게나 이어붙이면 의미가 뭉개진다.
    #
    # 실측 사고: judge_coverage(code=C73, disease=갑상선암, coverage=암진단비) 를
    # "C73 갑상선암 암진단비 보장되나요" 로 합성했더니 extract_coverage 가 **disease 를
    # 담보로 오인**해 "C73 ∈ 갑상선암 → 보장" 이 됐다. 정답은 "암진단비엔 미보장 →
    # 갑상선암으로 리다이렉트"다. 담보 특정성이 통째로 뒤집힌 것 — 슬롯 순서 하나로.
    # → 툴마다 **정해진 문형**으로 합성하고, code 가 있으면 disease 는 넣지 않는다.
    prod = (args.get("product") or "").strip()
    cov = (args.get("coverage") or "").strip()
    if ep == "coverage":
        code = (args.get("code") or "").strip()
        subject = code or (args.get("disease") or "").strip()
        q = f"{prod} {cov}로 {subject} 보장되나요?" if cov else f"{prod} {subject} 보장되나요?"
    elif ep == "payout":
        q = f"{prod} {cov} {args.get('cause') or ''} {args.get('period_bucket') or ''} 얼마?"
    elif ep == "waiting":
        q = f"{prod} {cov} 면책기간 얼마?"
    elif ep == "catalog":
        q = f"{prod}에 {cov} 담보 있어?"
    elif ep == "terms":
        q = f"{prod} 청약철회 언제까지?"
    else:
        q = f"{prod} {cov}".strip()
    q = " ".join(q.split()) or scenario
    # ⚠ 원 시나리오를 덧붙이지 **않는다**. 브랜드 스코프를 얻으려고 붙였다가 담보 추출이
    # 통째로 망가졌다 — 실측: "골든라이프 암진단비인데 C73 **갑상선암** 진단이면…" 을 붙이니
    # extract_coverage 가 시나리오 속 '갑상선암'을 담보로 잡아 "C73 ∈ 갑상선암 → 보장" 이
    # 됐다. 정답은 "암진단비엔 미보장 → 갑상선암 리다이렉트". 브랜드는 툴 인자 product 로
    # 받는 게 맞고(스키마에 추가함), 그래야 슬롯이 섞이지 않는다.
    try:
        d = _post(f"{API}/{ep}", {"query": q, "service_code": "01"})
    except Exception as e:
        return f"조회 실패: {type(e).__name__}"
    if not d.get("matched"):
        return "해당 정보를 결정론적으로 확인하지 못했습니다(확인 필요)."
    return str(d.get("answer"))


def _has_elem(answer: str, elem) -> bool:
    """요소 충족 판정. 리스트면 any-of(동의 표현), 문자열이면 그대로 포함 검사."""
    if isinstance(elem, (list, tuple)):
        return any(v in answer for v in elem)
    return elem in answer


def _elem_label(elem) -> str:
    return elem if isinstance(elem, str) else f"{elem[0]}(등 {len(elem)}표현)"


def _chat(messages: list, timeout: int = 180) -> dict:
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
            "temperature": 0, "max_tokens": 768,
            "chat_template_kwargs": {"enable_thinking": False}}
    return _post(f"{BASE}/chat/completions", body, timeout)["choices"][0]["message"]


def assemble_deterministic(facts: list[tuple[str, str]]) -> str:
    """수집된 툴 결과를 **결정론으로** 엮어 최종 답을 만든다 — LLM 재서술 없음.

    각 툴 결과는 이미 `format_coverage`·`format_payout` 등이 만든 소비자용 문장이다.
    여기서 하는 일은 순서 보존 + 중복 제거 + 이어붙이기뿐 — 사실을 **다시 쓰지 않는다**.
    그게 요점이다. LLM 에 재서술을 맡기면 "미보장"이 "보장됩니다"로 뒤집히고 100%가
    50%가 된다(첫 측정 실측). eval-and-golden §8 의 `format_answer` 와 같은 규율.
    """
    seen, out = set(), []
    for _tool, res in facts:
        r = res.strip()
        if not r or r in seen:
            continue
        seen.add(r)
        out.append(r)
    return "  ".join(out) if out else "확인할 수 있는 결정론 사실이 없습니다(확인 필요)."


def run_agent(scenario: str, show: bool = False,
              synth: str = "llm") -> tuple[list[str], str]:
    """에이전트 루프. (호출한 툴 순서, 최종 답).

    `synth` — 최종 답을 누가 만드나:
        "llm"           모델이 툴 결과를 읽고 자연어로 재서술 (기본, 흔한 에이전트 형태)
        "deterministic" 툴 결과를 그대로 조립 (LLM 재서술 없음)
    두 모드를 나란히 재면 **LLM 재서술이 얼마나 사실을 훼손하는지**가 분리된다.
    """
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": scenario}]
    called: list[str] = []
    facts: list[tuple[str, str]] = []
    for _ in range(MAX_HOPS):
        m = _chat(msgs)
        tcs = m.get("tool_calls") or []
        if not tcs:
            if synth == "deterministic":
                return called, assemble_deterministic(facts)
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
            facts.append((fn["name"], result))
            if show:
                print(f"      ↳ {fn['name']}({json.dumps(args, ensure_ascii=False)[:60]})")
                print(f"        → {result[:110]}")
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    if synth == "deterministic":
        return called, assemble_deterministic(facts)
    return called, "(홉 한도 초과)"


def main() -> int:
    show = "--show" in sys.argv
    synth = "deterministic" if "--deterministic" in sys.argv else "llm"
    print(f"  최종 합성: {synth}")
    rows = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    tool_ok = elem_ok = clean_ok = 0
    violations, tool_miss, elem_miss, said_forbidden = [], [], [], []

    for r in rows:
        print(f"\n  [{r['id']}] {r['scenario'][:52]}   ({r['pattern']})")
        try:
            called, answer = run_agent(r["scenario"], show, synth)
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
        # 요소는 **동의 표현 any-of** 를 허용한다. 항목이 리스트면 그 중 하나만 있으면 통과.
        #
        # 왜 — substring 정확 일치로만 보면 "표현 차이"가 "사실 오류"로 오분류된다.
        # 실측: MH04 답 "암진단비가 보장되지 않습니다" 는 판정이 **정확히 맞는데**
        # '미보장' 이라는 단어를 안 써서 실패로 셌다. MH08 "별표3 범위 밖 … 확인이 필요"
        # 도 의미상 판정불가인데 실패. 이러면 요소 recall 이 LLM 의 어휘 취향을 재는 꼴이
        # 되고, 진짜 위험(MH02 가 암진단자금 50% 를 섞은 것)이 같은 숫자에 묻힌다.
        # eval-and-golden §4 "자유서술은 정확 일치가 아니라 정규화/동의 처리" 그대로.
        lack = [_elem_label(e) for e in r["required_elements"] if not _has_elem(answer, e)]
        if not lack:
            elem_ok += 1
        else:
            elem_miss.append((r["id"], ", ".join(lack)))
        # ④ 있으면 안 되는 말 — 뒤집어 말하기·다른 담보 금액 혼입을 잡는다
        said = [e for e in r.get("must_not_contain", []) if e in answer]
        if not said:
            clean_ok += 1
        else:
            said_forbidden.append((r["id"], ", ".join(said)))
        print(f"      호출: {' → '.join(called) or '(없음)'}")
        print(f"      답  : {answer[:110].replace(chr(10), ' ')}")
        print(f"      툴{'✅' if not missing else '❌'} "
              f"요소{'✅' if not lack else '❌'} "
              f"금지문구{'✅' if not said else '❌'} "
              f"{'🚫금지툴 ' + ','.join(sorted(forbid)) if forbid else ''}")

    n = len(rows)
    print("\n" + "─" * 78)
    print(f"  n={n}   툴 recall {tool_ok}/{n} = {tool_ok / n:.2f}   "
          f"요소 recall {elem_ok}/{n} = {elem_ok / n:.2f}   "
          f"금지문구 {clean_ok}/{n} = {clean_ok / n:.2f}   "
          f"금지툴 위반 {len(violations)}건")
    for label, items in (("툴 누락", tool_miss), ("요소 누락", elem_miss),
                         ("🚫 금지문구", said_forbidden), ("🚫 금지툴", violations)):
        if items:
            print(f"  {label}: " + " / ".join(f"{i}({d})" for i, d in items))
    # 금지툴·금지문구 위반은 "없는 걸 있다고 답함" / "뒤집어 말함" — 오답보다 무겁게 본다
    return 1 if (violations or said_forbidden) else 0


if __name__ == "__main__":
    sys.exit(main())
