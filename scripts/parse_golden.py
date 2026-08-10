"""파싱 골든셋 — parse_clauses 출력을 사람 라벨 정답(조 수 · 조 제목)에 대조.

게이트(gate.py의 조 1..N sanity)와 별개인 '골든셋'. 게이트는 '연속인가'만 보지만,
골든은 '정확히 이 조들(번호·제목)인가'를 본다 → 잘못된 제목·병합·누락 조를 잡아
파싱 규칙을 바꿨을 때 순증(좋아졌나)/회귀(나빠졌나)를 판정한다.

용법: python3 scripts/parse_golden.py
"""
import os
import re
import json
import importlib.util

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_parse.jsonl")


def _load(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


st = _load("stage")


def predict(doc: str, field: str):
    """파싱 산출물(clauses)에서 필드 값. clause_count / title@N."""
    clauses = st.doc_clauses(doc)
    if field == "clause_count":
        return len(clauses)
    m = re.match(r"title@(\d+)", field)
    if m:
        c = next((c for c in clauses if c["jo"] == int(m.group(1))), None)
        return c["title"] if c else None
    return None


def _norm(s):
    return re.sub(r"\s", "", str(s)) if s is not None else None


def main():
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    ok = 0
    print(f"{'문서':<16}{'필드':<14}{'정답':<20}{'추출':<20}판정")
    print("-" * 78)
    for r in rows:
        pred = predict(r["doc"], r["field"])
        if r["field"] == "clause_count":
            hit = (pred == r["expected"])
        else:                                             # 제목은 정규화 후 포함
            hit = pred is not None and _norm(r["expected"]) in _norm(pred)
        ok += hit
        print(f"{r['doc'][:14]:<16}{r['field']:<14}{str(r['expected'])[:18]:<20}"
              f"{str(pred)[:18]:<20}{'✅' if hit else '❌'}")
    print("-" * 78)
    print(f"파싱 골든 {ok}/{len(rows)}  → 조 수·조 제목 정답 대조(게이트 sanity와 별개)")


if __name__ == "__main__":
    main()
