"""청크 품질 게이트 — RAG 청크(_chunks.json)의 전처리 완성도를 측정·게이팅(스택 불필요).

검색 골든(eval_retrieval)이 '검색이 잘 되나'를 잰다면, 이건 그 앞단 '청크가 깨끗한가'를 잰다.
청크 품질은 RAG 천장이라(노이즈 임베딩 = 검색·답변 오염), 색인 전 소스 청크를 게이팅한다.

측정(문서별):
- img   : 마크다운 이미지 태그 `![](...)` 잔해 (OCR은 별도 image 청크라 텍스트엔 순수 노이즈)
- page  : `<!-- page:N -->` 페이지 마커 잔해
- dots  : 점선 목차 잔해(`....`/`···`)
- br    : `<br>` (주로 OCR 표 셀 줄바꿈) — 임베딩 노이즈
- frag  : heading_path leaf가 문장 꼬리(…다./…니다.)나 빈 값 = heading 오검출(조 grouping 깨짐)
- 커버리지: RAG 색인(_chunks.json) vs 관계형 정제(clauses.jsonl) 존재 여부

게이트: img/page/dots/frag = 0 (전처리 수정 회귀 방지 — HARD). br은 WARN(정리 진행 중).
회귀(HARD 위반) 시 exit 1.

용법: python3 scripts/eval_chunk_quality.py   (또는 make chunk-quality)
"""
import os
import re
import sys
import json
import glob

HERE = os.path.dirname(__file__)
PROCESSED = os.path.join(HERE, "..", "data", "output", "processed")

_IMG = re.compile(r"!\[[^\]]*\]\(")
_PAGE = re.compile(r"<!--\s*page")
_DOTS = re.compile(r"\.{4,}|·{4,}|…{3,}")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_FRAG_TAIL = re.compile(r"(?:다|요|음|함|됨|임)\s*[.。]$")
_STRUCT = re.compile(r"제\s*\d+\s*[조관장절편]|별표|【")


def _leaf(c: dict) -> str:
    hp = c.get("heading_path") or []
    if isinstance(hp, str):
        hp = hp.split(" > ")
    return (hp[-1].strip() if hp else "")


def _is_fragment(leaf: str) -> bool:
    # 문장 꼬리로 끝나고 구조 마커(조/관/별표/【) 없으면 heading 오검출
    return bool(_FRAG_TAIL.search(leaf)) and not _STRUCT.search(leaf)


def scan(path: str) -> dict:
    ch = json.load(open(path, encoding="utf-8"))
    def cnt(rx):
        return sum(1 for c in ch if rx.search(c.get("content", "") or ""))
    frag = sum(1 for c in ch if _is_fragment(_leaf(c)))
    return {"n": len(ch), "img": cnt(_IMG), "page": cnt(_PAGE),
            "dots": cnt(_DOTS), "br": cnt(_BR), "frag": frag}


def main():
    files = sorted(glob.glob(os.path.join(PROCESSED, "*_chunks.json")))
    print("청크 품질 게이트 — RAG 청크 전처리 완성도")
    print("=" * 74)
    print(f"{'문서':<26}{'청크':>5}{'img':>5}{'page':>6}{'dots':>6}{'br':>5}{'frag':>6}")
    print("-" * 74)

    hard_fail = []      # img/page/dots/frag > 0
    br_total = 0
    for f in files:
        doc = os.path.basename(f).replace("_chunks.json", "")
        m = scan(f)
        br_total += m["br"]
        bad = m["img"] or m["page"] or m["dots"] or m["frag"]
        if bad:
            hard_fail.append(doc)
        print(f"{doc[:24]:<26}{m['n']:>5}{m['img']:>5}{m['page']:>6}"
              f"{m['dots']:>6}{m['br']:>5}{m['frag']:>6}")

    # 커버리지 — RAG 색인 vs 관계형 정제
    print("-" * 74)
    docs = ["라이나_간병인사용입원특약", "라이나_소득보장수술특약", "라이나_중환자실입원특약",
            "New치아보험_약관", "다이렉트늘안심입원비보험_약관"]
    rag = sum(1 for d in docs if os.path.exists(os.path.join(PROCESSED, f"{d}_chunks.json")))
    rel = sum(1 for d in docs if os.path.exists(os.path.join(PROCESSED, d, "clauses.jsonl")))
    both = sum(1 for d in docs if os.path.exists(os.path.join(PROCESSED, f"{d}_chunks.json"))
               and os.path.exists(os.path.join(PROCESSED, d, "clauses.jsonl")))
    print(f"커버리지: RAG색인 {rag}/5 · 관계형정제(clauses) {rel}/5 · 둘다 {both}/5")
    print("-" * 74)

    if hard_fail:
        print(f"❌ 전처리 회귀: {hard_fail} — img/page/dots/frag 중 0 아님 → exit 1")
        sys.exit(1)
    tag = "✅ 깨끗(br 0)" if br_total == 0 else f"⚠️ br {br_total}건 잔존(OCR 표 정리 대상, WARN)"
    print(f"✅ 하드 게이트 통과 (img·page·dots·frag = 0) · {tag}")


if __name__ == "__main__":
    main()
