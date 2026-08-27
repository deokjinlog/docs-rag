"""KB손해보험 공시실 약관 PDF 자동 수집기 (provenance 명확한 1차 출처).

KB 공시실(CG802030001.ec)은 JS 검색폼이지만, 약관 PDF는 **직접 URL 패턴**으로 노출된다(실측):
    https://www.kbinsure.co.kr/CG802030003.ec?fileNm={상품코드}_{공시버전}_{종류}.pdf
        종류: 1=보험약관 · 2=사업방법서 · 3=상품요약서 / 공시버전: 1,2,... (판매시작일별)
`detail(상품코드,gubun,seq)`가 폼을 CG802030002.ec(상세)로 POST → 상세페이지에 위 링크가 있음.

이 스크립트는 상품코드(또는 상품명 검색)로 **보험약관 PDF를 세션 쿠키로 직접 다운로드**하고,
provenance(회사·상품코드·공시URL·수집일시는 호출측 스탬프)를 남긴다. playwright 필요(project dep 아님).

용법:
  uv run --with playwright python scripts/fetch_kb_yakwan.py --code 22449
  uv run --with playwright python scripts/fetch_kb_yakwan.py --search 실손        # 상품명→코드 목록
  결과: data/input/KB_{코드}_약관.pdf  (+ stdout에 source URL)
"""
import os
import re
import sys
import argparse

HERE = os.path.dirname(__file__)
BASE = "https://www.kbinsure.co.kr"
LIST = f"{BASE}/CG802030001.ec"
PDF = f"{BASE}/CG802030003.ec?fileNm={{code}}_{{ver}}_1.pdf"      # _1=보험약관
OUT = os.path.join(HERE, "..", "data", "input")


def _ctx(p):
    b = p.chromium.launch(headless=True)
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.goto(LIST, timeout=30000, wait_until="domcontentloaded")
    pg.wait_for_timeout(1500)
    return b, ctx, pg


def search(term: str):
    """상품명 검색 → [(코드, 보종, 상품명)]. 코드 확인용."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b, ctx, pg = _ctx(p)
        pg.fill("#search_goods_nm", term)
        pg.get_by_text("조회", exact=True).first.click(timeout=8000)
        pg.wait_for_timeout(3000)
        out = []
        for r in pg.query_selector_all("table tbody tr"):
            tds = [c.inner_text().strip() for c in r.query_selector_all("td")]
            a = r.query_selector("a")
            oc = (a.get_attribute("onclick") or "") if a else ""
            m = re.search(r"detail\('([^']+)'", oc)
            if m and len(tds) >= 4:
                out.append((m.group(1), tds[0], tds[1], tds[3]))
        b.close()
        return out


def fetch(code: str) -> str | None:
    """상품코드 → 보험약관 PDF 다운로드(최신 공시버전 우선). 저장경로 반환(실패 None)."""
    from playwright.sync_api import sync_playwright
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        b, ctx, pg = _ctx(p)
        # 세션 확보 위해 상세페이지 한번 방문
        pg.evaluate(f"detail('{code}','c','1')")
        pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(1500)
        saved = None
        for ver in (2, 1):                                     # 최신(2) 우선, 없으면 1
            url = PDF.format(code=code, ver=ver)
            r = ctx.request.get(url, headers={"Referer": f"{BASE}/CG802030002.ec"})
            body = r.body() if r.status == 200 else b""
            if body[:4] == b"%PDF":
                path = os.path.join(OUT, f"KB_{code}_약관.pdf")
                open(path, "wb").write(body)
                print(f"✅ 다운로드: {path}  ({len(body):,} bytes)")
                print(f"   source: {url}")
                print(f"   provenance: KB손해보험 공시실 · 상품코드 {code} · 공시버전 {ver}")
                saved = path
                break
        if not saved:
            print(f"❌ 상품코드 {code} 보험약관 PDF 못 받음(코드/버전 확인)")
        b.close()
        return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search")
    ap.add_argument("--code")
    a = ap.parse_args()
    if a.search:
        rows = search(a.search)
        print(f"'{a.search}' 검색 {len(rows)}건 (코드 · 판매 · 보종 · 상품명):")
        for code, sale, bj, nm in rows[:20]:
            print(f"  {code}  [{sale}] {bj:<8} {nm[:44]}")
    elif a.code:
        fetch(a.code)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
