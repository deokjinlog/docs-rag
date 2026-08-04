"""면책 사유 목록 추출 — 소비자 "뭐가 안 돼요?"의 실제 사유(고의·전쟁·위험활동 등).

면책 조 매핑(담보→면책조)은 있으나 그건 '조가 있다'까지. 소비자가 진짜 궁금한 건 면책 조
본문의 '무엇이 안 되나' 목록이다. 면책 조 본문의 번호 항목(- N. 사유)을 표준 사유 태그로
정규화한다(표기변이 흡수). 조립기의 exclusion 엣지를 '제목'에서 '사유 목록'으로 깊게 만든다.

용법: python3 scripts/extract_exclusion_reasons.py       # 골든 채점
"""
import os
import re
import glob
import json
import importlib.util

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "..", "data", "eval", "golden_exclusion.jsonl")


def _load(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


pc = _load("parse_clauses")
EXCL_TITLE = ("지급하지 않", "지급하지아니", "보상하지 않", "보장하지 않")

# 표준 면책 사유 → 원문 키워드(표기변이 흡수). 표준약관 공통 사유 + 상품별.
REASON_KW = [
    ("고의", ["고의로"]),
    ("임신출산", ["임신", "출산", "산후"]),
    ("전쟁내란", ["전쟁", "무력행사", "혁명", "내란", "사변", "폭동"]),
    ("위험활동", ["전문등반", "글라이더", "스카이다이빙", "행글라이딩", "전문적인 등산", "모터보트", "자동차경기"]),
    ("무면허운전", ["무면허"]),
    ("음주운전", ["음주운전", "주취운전", "주취 상태"]),
    ("직업위험", ["직업, 직무", "직무 또는 동호회"]),
]


def _md(doc: str) -> str:
    path = next(p for p in glob.glob(os.path.join(HERE, "..", "data/output/raw/*.md")) if doc in p)
    return open(path, encoding="utf-8").read()


def _exclusion_body(doc: str) -> str:
    cl = pc.parse_clauses(_md(doc), "X")
    return "\n".join(c["text"] for c in cl if any(k in c["title"] for k in EXCL_TITLE))


def extract_exclusion_reasons(doc: str) -> list:
    """면책 조 본문 → 표준 면책 사유 태그 목록."""
    body = _exclusion_body(doc)
    return sorted({tag for tag, kws in REASON_KW if any(k in body for k in kws)})


def main():
    from collections import Counter
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    T = Counter()
    print(f"{'문서':<16}{'정답(사유)':<28}{'추출':<28}판정")
    print("-" * 92)
    for r in rows:
        pred = set(extract_exclusion_reasons(r["doc"]))
        exp = set(r["expected"])
        tp, fn, fp = len(pred & exp), len(exp - pred), len(pred - exp)
        T["TP"] += tp; T["FN"] += fn; T["FP"] += fp
        ok = "✅" if not (fn or fp) else f"❌(빠짐 {exp-pred or '-'}, 헛짚음 {pred-exp or '-'})"
        print(f"{r['doc'][:14]:<16}{','.join(sorted(exp)):<28}{','.join(sorted(pred)):<28}{ok}")
    print("-" * 92)
    rec = T["TP"] / (T["TP"] + T["FN"]) if (T["TP"] + T["FN"]) else 1.0
    prec = T["TP"] / (T["TP"] + T["FP"]) if (T["TP"] + T["FP"]) else 1.0
    print(f"사유 recall={rec:.2f} precision={prec:.2f} (TP{T['TP']} FN{T['FN']} FP{T['FP']})")


if __name__ == "__main__":
    main()
