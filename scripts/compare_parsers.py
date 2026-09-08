"""파서 비교 — ODL(현행) vs PaddleOCR-VL. 같은 문서, 같은 구조 지표로 나란히 잰다.

**왜 재나** — 어제 실측에서 ODL 이 **다단 읽기순서를 못 잡는다**는 게 드러났다.
하이브리드(docling)를 살려서 켜도 안 고쳐졌고(1fd1cfb), 오히려 java-only 와 다르게 틀렸다.
페이지를 통째로 보는 VL 파서라면 다를 수 있다 — 그걸 취향이 아니라 숫자로 정한다.

**채점 축** (골든 유무로 갈린다):

  ① 골든 있는 문서(라이나·New치아·다이렉트 5종) → `golden_parse` 50행 재사용.
     clause_count · title@N · structure · hang/ho/mok_count. **회귀 확인용** —
     VL 이 잘 되던 걸 깨뜨리면 안 된다.
  ② 골든 없는 문서(KB 다단) → 구조 불변식으로. 조 1..N 연속·조 개수·조 제목 수.
     `gate.py` 와 같은 축이다(도메인 불변식: 약관 조는 연속).
  ③ 읽기순서 — 정답 순서를 아는 합성 PDF 로 직접 대조. 사람이 눈으로 확인 가능.

용법:
    python3 scripts/compare_parsers.py --pdf <경로> [--pages 1-4]
    python3 scripts/compare_parsers.py --md-a <a.md> --md-b <b.md>   # 이미 뽑은 결과 비교
"""
import argparse
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ODL_URL = "http://localhost:5002"
RE_JO = re.compile(r"제\s*(\d+)\s*조")


def _post(url: str, payload: dict, timeout: int = 1800) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def structural_metrics(md: str) -> dict:
    """골든 없는 문서용 구조 지표 — gate.py 와 같은 도메인 불변식.

    조 번호가 **1..N 연속**이어야 한다는 게 핵심이다. 다단이 뒤섞이면 순서가 깨지므로
    '오름차순 비율' 이 그 손상을 직접 잡는다. 조 개수만 보면 순서 붕괴를 못 본다.
    """
    nums = [int(m.group(1)) for m in RE_JO.finditer(md)]
    uniq = sorted(set(nums))
    asc = sum(1 for a, b in zip(nums, nums[1:]) if b >= a)
    # 마크다운 heading 으로 승격된 조 제목 수 — 청킹 heading_path 의 재료라 품질에 직결
    headings = len(re.findall(r"^#{1,6}\s+\S", md, re.M))
    return {
        "chars": len(md),
        "조 언급": len(nums),
        "고유 조": len(uniq),
        "조 최대": max(uniq) if uniq else 0,
        "오름차순비": round(asc / (len(nums) - 1), 3) if len(nums) > 1 else 0.0,
        "1..N 연속": uniq == list(range(1, len(uniq) + 1)) if uniq else False,
        "heading": headings,
    }


def run_odl(pdf: str, out: str, hybrid: bool = False) -> str:
    body = {"input_path": pdf, "output_dir": out, "format": "markdown"}
    if hybrid:
        body |= {"hybrid": "docling-fast", "hybrid_mode": "full",
                 "hybrid_url": "http://localhost:5010", "hybrid_timeout": "900000"}
    _post(f"{ODL_URL}/convert", body)
    host = ROOT / "data" / out.removeprefix("/data/")
    mds = list(host.glob("*.md"))
    return mds[0].read_text(encoding="utf-8", errors="replace") if mds else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md-a", help="비교 대상 A (마크다운 경로)")
    ap.add_argument("--md-b", help="비교 대상 B")
    ap.add_argument("--label-a", default="ODL")
    ap.add_argument("--label-b", default="PaddleOCR-VL")
    a = ap.parse_args()
    if not (a.md_a and a.md_b):
        ap.error("--md-a 와 --md-b 를 함께 준다")

    ma = pathlib.Path(a.md_a).read_text(encoding="utf-8", errors="replace")
    mb = pathlib.Path(a.md_b).read_text(encoding="utf-8", errors="replace")
    sa, sb = structural_metrics(ma), structural_metrics(mb)

    print(f"  {'지표':<12}{a.label_a:>16}{a.label_b:>16}   판정")
    print("  " + "─" * 62)
    # 클수록 좋은 축과 그렇지 않은 축을 구분한다 — chars 는 많다고 좋은 게 아니다
    HIGHER_BETTER = {"조 언급", "고유 조", "오름차순비", "heading"}
    for k in sa:
        va, vb = sa[k], sb[k]
        mark = ""
        if k in HIGHER_BETTER and isinstance(va, (int, float)):
            mark = "→ B 우세" if vb > va else ("→ A 우세" if va > vb else "= 동일")
        elif k == "1..N 연속":
            mark = "→ B 우세" if (vb and not va) else ("→ A 우세" if (va and not vb) else "= 동일")
        print(f"  {k:<12}{str(va):>16}{str(vb):>16}   {mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
