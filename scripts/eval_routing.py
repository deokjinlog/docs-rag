"""라우팅 골든 — 5-type 쿼리 분류기 정확도(스택 불필요, 순수 정규식).

라우팅이 틀리면 검색 전략(dense/bm25 배수)이 어긋나 검색 품질이 샌다. RAGAS 실측에서
라우팅 정확도 20%가 나와(분류기가 '어떻게 정의/계산되나요'를 procedure로 오분류) 이 게이트를 둔다.

classify_query()를 골든 질의에 직접 돌려 expected query_type과 대조. classifier.py는
순수 의존(re/dataclass/enum)이라 패키지 __init__(모델 로딩)을 안 건드리게 파일에서 직접 로드.

지표: 전체 정확도 + 유형별 혼동. baseline 회귀 시 exit 1.
용법: python3 scripts/eval_routing.py   (또는 make eval-routing)
"""
import os
import sys
import json
import importlib.util

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_routing.jsonl")
BASELINE = os.path.join(HERE, "..", "data", "eval", "routing_baseline.json")
_CLF_PATH = os.path.join(HERE, "..", "src", "v1", "rag", "classifier.py")

# classifier.py를 패키지 __init__ 없이 단독 로드(모델 로딩 회피).
_spec = importlib.util.spec_from_file_location("_classifier", _CLF_PATH)
_clf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_clf)
classify_query = _clf.classify_query


def main():
    update = "--update-baseline" in sys.argv
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]

    print("라우팅 골든 — 5-type 분류기 정확도")
    print("=" * 78)
    print(f"{'질의':<44}{'기대':<16}{'실제':<16}")
    print("-" * 78)
    ok = 0
    miss = []
    for r in rows:
        got = classify_query(r["query"]).query_type.value
        exp = r["expected_type"]
        hit = got == exp
        ok += hit
        if not hit:
            miss.append((r["query"], exp, got))
        mark = "✅" if hit else "❌"
        print(f"{r['query'][:42]:<44}{exp:<16}{got:<16}{mark}")

    n = len(rows)
    acc = ok / n
    print("-" * 78)
    print(f"  라우팅 정확도 = {ok}/{n} = {acc:.3f}")
    if miss:
        print("  오분류:")
        for q, e, g in miss:
            print(f"    ✗ {q[:40]:<42} {e} → {g}")

    cur = {"accuracy": acc, "n": n}
    if update:
        json.dump(cur, open(BASELINE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n✅ baseline 저장: accuracy={acc:.3f}")
        return
    if not os.path.exists(BASELINE):
        print("\n⚠️  baseline 없음 — 첫 측정. --update-baseline로 고정 권장.")
        return
    base = json.load(open(BASELINE, encoding="utf-8"))
    print("-" * 78)
    if acc < base["accuracy"] - 1e-9:
        print(f"❌ 라우팅 회귀: {base['accuracy']:.3f} → {acc:.3f} → exit 1")
        sys.exit(1)
    tag = "순증" if acc > base["accuracy"] + 1e-9 else "동률"
    print(f"✅ 무회귀({tag}) — baseline {base['accuracy']:.3f} 대비 유지/향상")


if __name__ == "__main__":
    main()
