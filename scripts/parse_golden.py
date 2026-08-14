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
pc = _load("parse_clauses")

_SUBITEM_RE = re.compile(r"(hang|ho|mok)_count@(\d+)")


def _structure(clauses) -> str:
    """조 구조 무결성 한 줄 판정 — 1..N 연속·중복·빈 제목. 정상이면 'clean',
    아니면 이상을 서술(파서 회귀 시 clean이 깨져 골든이 잡음). gate.py sanity를
    골든 수준으로 끌어올려 '구조가 이 상태로 고정'임을 잠근다(파싱 안정도)."""
    jos = [c["jo"] for c in clauses]
    if not jos:
        return "empty:조 0개"
    seen = {}
    for j in jos:
        seen[j] = seen.get(j, 0) + 1
    dup = sorted(j for j, n in seen.items() if n > 1)
    gap = sorted(set(range(min(jos), max(jos) + 1)) - set(jos))
    blank = sorted(c["jo"] for c in clauses if not (c.get("title") or "").strip())
    parts = []
    if gap:   parts.append(f"gap:{gap}")
    if dup:   parts.append(f"dup:{dup}")
    if blank: parts.append(f"blank:{blank}")
    return "clean" if not parts else " ".join(parts)


def predict(doc: str, field: str):
    """파싱 산출물(clauses)에서 필드 값. clause_count / title@N / structure."""
    clauses = st.doc_clauses(doc)
    if field == "clause_count":
        return len(clauses)
    if field == "structure":
        return _structure(clauses)
    m = _SUBITEM_RE.match(field)                          # 항/호/목 세분 count
    if m:
        kind, jo = m.group(1), int(m.group(2))
        c = next((c for c in clauses if c["jo"] == jo), None)
        if not c:
            return None
        nh, nho, nmok = pc.subitem_counts(pc.parse_subitems(c["text"]))
        return {"hang": nh, "ho": nho, "mok": nmok}[kind]
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
        if r["field"] in ("clause_count", "structure") or _SUBITEM_RE.match(r["field"]):
            hit = (pred == r["expected"])                 # 정확 일치(수·구조)
        else:                                             # 제목은 정규화 후 포함
            hit = pred is not None and _norm(r["expected"]) in _norm(pred)
        ok += hit
        print(f"{r['doc'][:14]:<16}{r['field']:<14}{str(r['expected'])[:18]:<20}"
              f"{str(pred)[:18]:<20}{'✅' if hit else '❌'}")
    print("-" * 78)
    print(f"파싱 골든 {ok}/{len(rows)}  → 조 수·조 제목 정답 대조(게이트 sanity와 별개)")


if __name__ == "__main__":
    main()
