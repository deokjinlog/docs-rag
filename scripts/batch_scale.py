"""대용량 스케일 배치 — KB 약관 N개 코드열거 수집 + pymupdf 고속 파싱 + 집계 메트릭.

결정론 파싱은 텍스트 연산(GPU·OCR 불필요)이라 대용량이 하드웨어에 안 막힌다. ODL(느림) 대신
pymupdf 텍스트층(빠름)으로 추출·재구성(page→단→y)해 조·특약을 센다. "코퍼스 대량 처리" 실증.

수집: KB 공시실 직접 URL(CG802030003.ec?fileNm=코드_버전_1.pdf) 코드열거(검색 우회, respectful 딜레이).
처리: pymupdf 재구성 → parse_clauses(조) + detect_subcontracts(특약) + 한글비율(품질 게이트).
용법: uv run --with pymupdf python scripts/batch_scale.py [목표수=300]
"""
import os, re, sys, time, json, hashlib, urllib.request, importlib.util

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "..", "data", "_batch_scale")
URL = "https://www.kbinsure.co.kr/CG802030003.ec?fileNm={}_{}_1.pdf"
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 300
# 보종 다양성 위해 여러 코드 범위 인터리브
RANGES = [(21000, 21600), (22000, 23500)]


def _mod(n):
    s = importlib.util.spec_from_file_location(n, os.path.join(HERE, f"{n}.py"))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


pc = _mod("parse_clauses")


def _codes():
    for lo, hi in RANGES:
        for c in range(lo, hi):
            yield c


def collect(target):
    os.makedirs(OUT, exist_ok=True)
    seen, tried = {}, 0
    for code in _codes():
        if len(seen) >= target:
            break
        tried += 1
        for ver in (2, 1):
            try:
                b = urllib.request.urlopen(urllib.request.Request(
                    URL.format(code, ver), headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read()
                if b[:4] == b"%PDF":
                    h = hashlib.md5(b).hexdigest()
                    if h not in seen:
                        p = os.path.join(OUT, f"{code}.pdf")
                        open(p, "wb").write(b); seen[h] = (code, p)
                    break
            except Exception:
                pass
        time.sleep(0.4)                                   # respectful
        if len(seen) % 25 == 0 and len(seen):
            print(f"  수집 {len(seen)}/{target} (시도 {tried})", flush=True)
    return list(seen.values())


def parse_pdf(path):
    # pymupdf 텍스트층 고속 추출. sort=True로 읽기순서 정렬(ODL 노드용 커스텀 재정렬은 pymupdf
    # 블록엔 오작동해 제거). 주의: 이 고속 경로는 glyph 파편화 PDF에서 조 제목 괄호(【】·())를
    # 복원 못 해 결정론 조 파싱이 실패한다(실측 300개 중 깨끗 2개) → 축1(처리량)만 유효, 축2(구조
    # 파싱)는 ODL 통과가 전제. corpus-provenance.md '대용량 스케일 테스트' 참조.
    import pymupdf
    d = pymupdf.open(path); npages = len(d)
    text = "\n".join(pg.get_text(sort=True) for pg in d)
    d.close()
    clauses = pc.parse_clauses(text, "X")
    subs = pc.detect_subcontracts(text) if hasattr(pc, "detect_subcontracts") else []
    hangul = len(re.findall(r"[가-힣]", text)); nonsp = len(re.sub(r"\s", "", text)) or 1
    return {"pages": npages, "jo": len(clauses), "teukyak": len(subs),
            "hangul_ratio": hangul / nonsp, "chars": len(text)}


def main():
    t0 = time.time()
    print(f"═══ 대용량 배치 스케일 — 목표 {TARGET} KB 약관 ═══", flush=True)
    docs = collect(TARGET)
    print(f"수집 완료: {len(docs)} distinct 약관 ({time.time()-t0:.0f}s)", flush=True)
    print("─── pymupdf 고속 파싱 ───", flush=True)
    agg = {"docs": 0, "pages": 0, "jo": 0, "teukyak": 0, "chars": 0, "gate_pass": 0, "fail": 0}
    for i, (code, path) in enumerate(docs):
        try:
            r = parse_pdf(path)
            agg["docs"] += 1; agg["pages"] += r["pages"]; agg["jo"] += r["jo"]
            agg["teukyak"] += r["teukyak"]; agg["chars"] += r["chars"]
            if r["hangul_ratio"] >= 0.30 and r["jo"] >= 1:
                agg["gate_pass"] += 1
        except Exception as e:
            agg["fail"] += 1
        if (i + 1) % 50 == 0:
            print(f"  파싱 {i+1}/{len(docs)}", flush=True)
    gb = sum(os.path.getsize(p) for _, p in docs) / 1024**3
    dt = time.time() - t0
    print("\n" + "=" * 60)
    print("대용량 스케일 결과 (KB 약관 자동 처리)")
    print("=" * 60)
    print("── 축1: 수집·추출 처리량 (고속 경로 성공) ──")
    print(f"  약관 문서    : {agg['docs']:>8,} 개")
    print(f"  총 페이지    : {agg['pages']:>8,} p")
    print(f"  총 문자      : {agg['chars']:>8,} 자")
    print(f"  용량         : {gb:>8.2f} GB")
    print(f"  파싱 실패    : {agg['fail']}")
    print(f"  총 소요      : {dt:.0f}s ({dt/max(agg['docs'],1):.1f}s/약관)")
    print("── 축2: 결정론 구조 파싱 (이 PDF엔 ODL 필요·고속경로 한계) ──")
    print(f"  총 조(條)    : {agg['jo']:>8,} 개  (깨끗 파싱 소수 — glyph 파편화로 조제목 괄호 소실)")
    print(f"  총 특약      : {agg['teukyak']:>8,} 개")
    print(f"  게이트 통과  : {agg['gate_pass']}/{agg['docs']} ({agg['gate_pass']/max(agg['docs'],1)*100:.0f}%)"
          f"  ※ 구조 파싱은 ODL clean.md 전제 — corpus-provenance.md 참조")
    json.dump({**agg, "gb": round(gb, 2), "sec": round(dt)},
              open(os.path.join(OUT, "_metrics.json"), "w"), ensure_ascii=False, indent=2)
    print("  → data/_batch_scale/_metrics.json 저장")


if __name__ == "__main__":
    main()
