"""골든셋 채점기 (실패분석 루프 4단계) — 추출 결과를 사람이 라벨한 정답지에 대고 채점.

실패 로그(어디가 구멍인지 찾기)와 별개인 '검증' 도구. data/eval/golden_covname.jsonl의
정답과 실제 추출을 대조해 precision/recall을 낸다. 규칙을 바꾼 뒤 이걸 돌려서 recall이
올랐나(순증) / 기존이 깨졌나(회귀)를 판정 = 회귀 방지 안전벨트.

용법: python3 scripts/golden_eval.py
"""
import re
import json
import pathlib
import importlib.util

_HERE = pathlib.Path(__file__).parent


def _mod(n):
    s = importlib.util.spec_from_file_location(n, _HERE / f"{n}.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


pc = _mod("parse_clauses")
ep = _mod("extract_product")
GOLDEN = _HERE.parent / "data" / "eval" / "golden_covname.jsonl"


def predict(doc: str, section: str, field: str):
    """실제 추출기(extract_product)를 돌려 해당 필드 값을 얻는다."""
    md = open(f"data/output/raw/{doc}.md", encoding="utf-8").read()
    secs = pc.split_sections(md)
    if len(secs) == 1:                                   # 단일 문서(라이나)
        p = ep.extract_product(md, "X", "x")
    elif section == "":                                  # 복합문서의 보통약관
        sec = next(s for s in secs if not s["parent"])
        p = ep.extract_product(md, "X", "x", region=sec["region"])
    else:                                                # 복합문서의 개별 특약
        sec = next(s for s in secs if section in s["name"])
        p = ep.extract_product(md, "X", "x", region=sec["region"],
                               parent_id="P", name=sec["name"])
    return p.get(field)


def main():
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    tp = fp = fn = tn = 0
    print(f"{'문서/구간':<34}{'정답':<18}{'추출':<18}판정")
    print("-" * 78)
    for r in rows:
        gold = r["expected"]
        pred = predict(r["doc"], r["section"], r["field"])
        if gold and pred == gold:
            tp += 1; v = "✅ TP"
        elif gold and pred is None:
            fn += 1; v = "❌ FN(놓침)"
        elif gold and pred != gold:
            fp += 1; v = f"❌ FP(틀림)"
        elif gold is None and pred is None:
            tn += 1; v = "✅ TN(맞게비움)"
        else:
            fp += 1; v = "❌ FP(헛짚음)"
        label = f"{r['doc'][:12]}/{r['section'] or '(전체)'}"
        print(f"{label:<34}{str(gold):<18}{str(pred):<18}{v}")

    rec = tp / (tp + fn) if (tp + fn) else 1.0
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    print("-" * 78)
    print(f"TP={tp} FN={fn} FP={fp} TN={tn}  →  recall={rec:.2f}  precision={prec:.2f}")
    print(f"(recall = 정답 있는 것 중 맞게 뽑은 비율 = {tp}/{tp+fn})")


if __name__ == "__main__":
    main()
