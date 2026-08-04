"""Bronze→Silver 스테이징 (medallion) — 원본 추출물을 정제·구조화한 '재청킹·재추출 소스'.

  bronze  data/output/raw/{doc}.md · .json · _images   (ODL 원본, immutable)
  silver  data/output/silver/{doc}/                     (이 파일이 생성)
            clean.md       안전 최소 정규화(BOM·트레일링공백·과다빈줄) — 추출 불변
            clauses.jsonl  조 파싱(jo·title·text) 캐시 — 관계형·청킹 공용(파싱 1회)
            profile.json   포맷 프로파일(전각/반각·복합문서·페이지수)
  gold    PostgreSQL·Qdrant                              (청크·벡터·관계형, 파일 아님)

정제(normalize)는 '측정 후 확장' 원칙 — 지금은 추출 결과를 안 바꾸는 안전 정규화만.
공격적 정규화(전각→반각 통일 등)는 자체 골든 통과 후 여기에 얹는다(precision-first).

공용 resolver: doc_md(doc)/doc_clauses(doc) — silver 있으면 silver, 없으면 bronze 폴백.
용법: python3 scripts/stage.py        # 전 문서 silver/ 생성
"""
import os
import re
import glob
import json
import importlib.util

HERE = os.path.dirname(__file__)
BRONZE = os.path.join(HERE, "..", "data", "output", "raw")
SILVER = os.path.join(HERE, "..", "data", "output", "silver")


def _load(name):
    s = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


pc = _load("parse_clauses")


def _bronze_md(doc: str) -> str:
    p = next(x for x in glob.glob(os.path.join(BRONZE, "*.md")) if doc in x)
    return open(p, encoding="utf-8").read()


def normalize(md: str) -> str:
    """안전 최소 정규화 — 추출 결과 불변(BOM·트레일링 공백·과다 빈줄만). 확장 지점."""
    md = md.replace("﻿", "")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md


def profile(doc: str, md: str) -> dict:
    """포맷 프로파일 — 회사 넘어 일반화되는 '형식' 축(구조 아님)."""
    full = len(re.findall(r"제\s*\d+\s*조\s*【", md))     # 조 제목 괄호 스타일로 판정
    half = len(re.findall(r"제\s*\d+\s*조\s*\(", md))     # (별표의 【】 오탐 회피)
    return {
        "doc": doc,
        "format": "전각【】" if full >= half else "반각()",
        "clause_title_full": full, "clause_title_half": half,
        "compound": md.count("특별약관") > 5,          # 보통약관+특약 복합문서
        "page_markers": md.count("<!-- page:"),
    }


def stage(doc: str) -> int:
    md = normalize(_bronze_md(doc))
    d = os.path.join(SILVER, doc)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "clean.md"), "w", encoding="utf-8").write(md)
    clauses = pc.parse_clauses(md, "X")
    with open(os.path.join(d, "clauses.jsonl"), "w", encoding="utf-8") as f:
        for c in clauses:
            f.write(json.dumps({"jo": c["jo"], "title": c["title"], "text": c["text"]},
                               ensure_ascii=False) + "\n")
    json.dump(profile(doc, md), open(os.path.join(d, "profile.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return len(clauses)


def stage_all():
    docs = [os.path.basename(p)[:-3] for p in glob.glob(os.path.join(BRONZE, "*.md"))]
    for doc in sorted(docs):
        n = stage(doc)
        p = json.load(open(os.path.join(SILVER, doc, "profile.json"), encoding="utf-8"))
        print(f"  silver/{doc[:18]:<20} 조 {n:>3}  {p['format']}  복합={p['compound']}")
    return docs


# ── 공용 resolver (스크립트가 silver 우선, 없으면 bronze 폴백) ──────────────
def doc_md(doc: str) -> str:
    p = os.path.join(SILVER, doc, "clean.md")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else _bronze_md(doc)


def doc_clauses(doc: str) -> list:
    p = os.path.join(SILVER, doc, "clauses.jsonl")
    if os.path.exists(p):
        return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    return pc.parse_clauses(doc_md(doc), "X")


if __name__ == "__main__":
    print("bronze → silver 스테이징:")
    stage_all()
