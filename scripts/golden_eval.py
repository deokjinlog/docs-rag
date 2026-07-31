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
GOLDEN = _HERE.parent / "data" / "eval" / "golden.jsonl"


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


def _judge(gold, pred):
    """결정론 채점 — LLM 아님. 값 대조로 TP/FN/FP/TN."""
    if gold and pred == gold:      return "TP", "✅ TP"
    if gold and pred is None:      return "FN", "❌ FN(놓침)"
    if gold and pred != gold:      return "FP", "❌ FP(틀림)"
    if gold is None and pred is None: return "TN", "✅ TN(맞게비움)"
    return "FP", "❌ FP(헛짚음)"


def main():
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    from collections import Counter
    per_field = {}                                     # field → Counter(TP/FN/FP/TN)
    print(f"{'문서/구간':<22}{'필드':<20}{'정답':<16}{'추출':<16}판정")
    print("-" * 86)
    for r in rows:
        pred = predict(r["doc"], r["section"], r["field"])
        cat, v = _judge(r["expected"], pred)
        per_field.setdefault(r["field"], Counter())[cat] += 1
        label = f"{r['doc'][:8]}/{r['section'] or '전체'}"
        print(f"{label:<22}{r['field']:<20}{str(r['expected']):<16}{str(pred):<16}{v}")

    print("-" * 86)
    print("필드별 precision/recall (결정론 채점):")
    T = Counter()
    for f, c in per_field.items():
        T += c
        rec = c["TP"] / (c["TP"] + c["FN"]) if (c["TP"] + c["FN"]) else 1.0
        prec = c["TP"] / (c["TP"] + c["FP"]) if (c["TP"] + c["FP"]) else 1.0
        print(f"  {f:<22} recall={rec:.2f}  precision={prec:.2f}  "
              f"(TP{c['TP']} FN{c['FN']} FP{c['FP']} TN{c['TN']})")
    rec = T["TP"] / (T["TP"] + T["FN"]) if (T["TP"] + T["FN"]) else 1.0
    prec = T["TP"] / (T["TP"] + T["FP"]) if (T["TP"] + T["FP"]) else 1.0
    print(f"  {'─ 전체':<22} recall={rec:.2f}  precision={prec:.2f}")


if __name__ == "__main__":
    main()
