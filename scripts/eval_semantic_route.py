"""시맨틱 라우터 채점 — 경로 **의도** 분류 정확도 (오프라인, HTTP 없음).

`eval_sql_routing.py` 와 무엇이 다른가:

    eval_sql_routing   /answer 를 HTTP 로 때려 **최종 route**(sql/rag)를 본다.
                       의도가 맞아도 데이터가 없으면 RAG 로 폴백하므로, 의도 오류와
                       데이터 가용성 폴백이 한 숫자에 뭉친다.
    이 스크립트        모듈만 불러 **intent** 를 본다. 스택은 임베딩 모델만 필요하고
                       API·DB·Qdrant 는 안 뜬다. 의도 분류만 따로 잰다.

두 축을 가르는 이유는 실패 원인이 다르기 때문이다 — 의도를 틀리면 **라우터**를 고치고,
의도는 맞는데 값이 없으면 **추출·적재**를 고친다. 한 숫자로 보면 뭘 고칠지 못 짚는다.

precision-first 채점: 라우터가 None(판정불가)을 낸 건 **오답이 아니라 기권**으로 센다.
기권하면 기존 정규식·RAG 흐름이 그대로 받으므로 회귀가 아니다. 진짜 손해는 **오라우팅**
(다른 경로를 확신 있게 고르는 것)이고, 그게 결정론 경로에서 확신에 찬 오답을 만든다.

용법:
    python3 scripts/eval_semantic_route.py              # 채점
    python3 scripts/eval_semantic_route.py --dist       # 점수 분포(임계 보정용)
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.v1.rag import semantic_router as sr   # noqa: E402

GOLDEN = pathlib.Path(__file__).resolve().parent.parent / "data/eval/golden_sql_routing.jsonl"


def _golden_path() -> pathlib.Path:
    """--golden <경로> 로 평가셋 교체. held-out 셋 채점용(train-on-test 분리)."""
    if "--golden" in sys.argv:
        return pathlib.Path(sys.argv[sys.argv.index("--golden") + 1])
    return GOLDEN


def _rows():
    return [json.loads(l) for l in _golden_path().read_text(encoding="utf-8").splitlines() if l.strip()]


def _encoder():
    from src.v1.utils.embedding import embed_texts
    return embed_texts


def main() -> int:
    rows = [r for r in _rows() if r.get("intent")]
    if not rows:
        print(f"골든에 intent 축이 없다: {_golden_path()}", file=sys.stderr)
        return 2
    encode = _encoder()
    dist_mode = "--dist" in sys.argv

    hit = abstain = wrong = 0
    wrongs, abstains = [], []
    print(f"{'질의':<44}{'정답':<10}{'예측':<10}{'점수':<8}{'margin':<8}")
    print("-" * 84)
    for r in rows:
        got = sr.route_semantic(r["query"], encode=encode)
        exp = r["intent"]
        if got is None:
            abstain += 1
            abstains.append((r["query"], exp))
            mark, pred, sc, mg = "➖", "(기권)", "", ""
        else:
            pred, sc, mg = got.route, f"{got.score:.3f}", f"{got.margin:.3f}"
            if got.route == exp:
                hit += 1
                mark = "✅"
            else:
                wrong += 1
                wrongs.append((r["query"], exp, got.route, got.score, got.margin))
                mark = "❌"
        print(f"{r['query'][:42]:<44}{exp:<10}{pred:<10}{sc:<8}{mg:<8}{mark}")

    n = len(rows)
    print("-" * 84)
    # 정확도 = 정답/전체, 오라우팅률 = 확신 있게 틀린 비율(이게 진짜 위험 지표)
    print(f"  n={n}  정답 {hit} · 기권 {abstain} · 오라우팅 {wrong}"
          f"   정확도={hit / n:.3f}  오라우팅률={wrong / n:.3f}")
    if wrongs:
        print("  ❌ 오라우팅:")
        for q, e, g, s, m in wrongs:
            print(f"     {q[:38]:<40} 정답 {e} ← {g} (score {s:.3f}, margin {m:.3f})")
    if abstains:
        print("  ➖ 기권(기존 흐름이 받음): " + " / ".join(f"{q[:18]}→{e}" for q, e in abstains))

    if dist_mode:
        print("\n  [점수 분포 — 임계 보정용]")
        scored = []
        for r in rows:
            qv = encode([r["query"]])[0]
            best: dict[str, float] = {}
            for route, vec in sr._get_cache(encode):
                s = sr._dot(qv, vec)
                if s > best.get(route, -1.0):
                    best[route] = s
            ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
            scored.append((r["intent"], ranked[0], ranked[1]))
        correct = [(a[1], a[1] - b[1]) for exp, a, b in scored if a[0] == exp]
        incorrect = [(a[1], a[1] - b[1]) for exp, a, b in scored if a[0] != exp]
        for label, xs in (("1위가 정답", correct), ("1위가 오답", incorrect)):
            if not xs:
                continue
            ss = sorted(x[0] for x in xs)
            ms = sorted(x[1] for x in xs)
            print(f"    {label:<10} n={len(xs):<3} score min={ss[0]:.3f} p50={ss[len(ss)//2]:.3f} "
                  f"max={ss[-1]:.3f} | margin min={ms[0]:.3f} p50={ms[len(ms)//2]:.3f}")
    # ── 회귀 판정 ────────────────────────────────────────────────────────────
    # 게이트는 **오라우팅률**이다. 기권은 회귀가 아니고(기존 흐름이 받음), 정확도는
    # 기권이 늘면 같이 떨어져서 게이트로 쓰면 "안전해질수록 실패"가 된다.
    # baseline 이 있으면 그것과 대조, 없으면 오라우팅 0 을 요구한다.
    base = pathlib.Path(__file__).resolve().parent.parent / "data/eval/routing_semantic_baseline.json"
    rate = wrong / n
    if base.exists() and "--golden" in sys.argv:
        b = json.loads(base.read_text(encoding="utf-8"))
        if pathlib.Path(b.get("golden", "")).name == _golden_path().name:
            lim = float(b["misroute_rate"])
            ok = rate <= lim + 1e-9
            print(f"  {'✅ 무회귀' if ok else '❌ 회귀'} — 오라우팅률 {rate:.3f} "
                  f"vs baseline {lim:.3f} ({b['measured']} 측정, precision {b['precision']})")
            return 0 if ok else 1
    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
